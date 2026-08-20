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
import hashlib
import json
import shutil
import signal
import socket as socket_mod
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_startup import start_run
from dsl41.runner_admission import PROTOCOL_VERSION, addressed_key
from dsl41.runner_control import ControlServer, command, read_for, revision_in
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, RealClock
from dsl41.runner_journal import read_journal
from dsl41.runner_scheduler import Scheduler

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


def _versioned(request: dict) -> dict:
    """Stamp the protocol version unless the caller named one itself -- the
    same rule the shipped clients follow, so a test that DOES name one can
    still exercise the server's refusal (DL-90)."""
    return request if "v" in request else {**request, "v": PROTOCOL_VERSION}


def _sync_control_call(sock_path: Path, request: dict, timeout: float = 5.0) -> dict:
    """Blocking control-socket round trip -- ONLY for the subprocess test,
    where the engine lives in a different process/loop."""
    conn = socket_mod.socket(socket_mod.AF_UNIX)
    conn.settimeout(timeout)
    conn.connect(str(sock_path))
    conn.sendall(json.dumps(_versioned(request)).encode("utf-8") + b"\n")
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
        writer.write(json.dumps(_versioned(request)).encode("utf-8") + b"\n")
        await writer.drain()
        line = await reader.readline()
        return json.loads(line)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _read_revision(sock_path: Path, key: str) -> tuple[str, int, int]:
    """The read half of a read-then-write: the ss6 header this log answers
    with, and the revision the addressed entity is at right now. The rule for
    WHICH query answers a key, and where the revision sits in its answer,
    comes from runner_control -- the tests are a third client of it, not a
    third place that knows it (DL-91)."""
    read = await _control_call(sock_path, read_for(key))
    return str(read.get("baseline_id") or ""), int(read.get("epoch") or 0), revision_in(read, key)


def _body(response: dict, engine: Engine) -> dict:
    """Split the ss6 read header off an answer, checking it on the way.

    Every answer publishes `baseline_id`, `epoch` and `applied_index` -- so a
    whole-response assertion that spelled them out would repeat the same
    three lines in every test that pins a shape. This says it once, for all
    of them, and the tests below keep asserting exact shapes rather than
    softening to subset checks."""
    header = {key: response.pop(key, None) for key in ("baseline_id", "epoch", "applied_index")}
    assert header == {
        "baseline_id": engine.baseline_id,
        "epoch": engine.epoch,
        "applied_index": engine.frontiers.applied_index,
    }
    return response


def _sync_sendevent(sock_path: Path, verb: str, **payload: object) -> dict:
    """The blocking twin of `_sendevent`, for the subprocess test."""
    key = addressed_key(verb, payload)
    read = _sync_control_call(sock_path, read_for(key))
    return _sync_control_call(
        sock_path,
        command(
            verb,
            payload,
            key=key,
            revision=revision_in(read, key),
            baseline_id=str(read.get("baseline_id") or ""),
            epoch=int(read.get("epoch") or 0),
        ),
    )


async def _sendevent(
    sock_path: Path, verb: str, *, expect: int | None = None, **payload: object
) -> dict:
    """One mutation, composed the way ss0 requires: read the addressed
    entity's revision, then NAME it. `expect=` pins a revision by hand, which
    is what a test of a LOST race needs -- an auto-read can only ever agree
    with itself."""
    key = addressed_key(verb, payload)
    baseline, epoch, current = await _read_revision(sock_path, key)
    return await _control_call(
        sock_path,
        command(
            verb,
            payload,
            key=key,
            revision=current if expect is None else expect,
            baseline_id=baseline,
            epoch=epoch,
            claimed_actor="tests@localhost",
        ),
    )


async def _wait_for_async(predicate, timeout_s: float = 3.0, interval_s: float = 0.02):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {predicate}")


async def _serve(
    run_root: Path,
    text: str,
    *,
    adapter: FakeAdapter | None = None,
    scheduler=None,
    spec_texts: dict[str, str] | None = None,
    estate_fingerprint: dict[str, str] | None = None,
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
    server = ControlServer(
        engine,
        run_root / "control.sock",
        spec_texts=spec_texts,
        estate_fingerprint=estate_fingerprint,
    )
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
            resp = await _sendevent(server.path, "STARTJOB", job="sv_job")
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
            resp = await _sendevent(server.path, "STARTJOB", job="does-not-exist")
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
            resp = await _sendevent(server.path, "SET_GLOBAL", name="FLAG", value="go")
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
            bad = await _sendevent(server.path, "CHANGE_STATUS", job="cs_job", status="BOGUS")
            assert bad["ok"] is False

            good = await _sendevent(
                server.path,
                "CHANGE_STATUS",
                job="cs_job",
                status="SUCCESS",
                exit_code=0,
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
            await _sendevent(server.path, "ON_HOLD", job="jr_job")
            await _sendevent(server.path, "OFF_HOLD", job="jr_job")
            await _sendevent(server.path, "SET_GLOBAL", name="G1", value="v1")

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
            await _sendevent(server.path, "ON_HOLD", job="hold_job")

            async def held() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "hold_job"})
                return r["jobs"]["hold_job"]["on_hold"] is True

            await _wait_for_async(held)

            await _sendevent(server.path, "OFF_HOLD", job="hold_job")

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


def test_startjob_via_control_socket_tags_cause_and_started_by_control(short_root: Path) -> None:
    """(DL-68): an operator sendevent STARTJOB is distinguishable from a
    scheduler tick -- the trace cause reads "STARTJOB event (control)" and
    the status verb serves it as started_by (null before any start)."""
    text = "insert_job: prov_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            before = await _control_call(server.path, {"cmd": "status", "job": "prov_job"})
            assert before["jobs"]["prov_job"]["started_by"] is None

            await _sendevent(server.path, "STARTJOB", job="prov_job")

            async def done() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "prov_job"})
                return r["jobs"]["prov_job"]["status"] == "SUCCESS"

            await _wait_for_async(done)
            after = await _control_call(server.path, {"cmd": "status", "job": "prov_job"})
            assert after["jobs"]["prov_job"]["started_by"] == "STARTJOB event (control)"
            starting = next(
                t for t in engine.oracle.trace() if t.transition == "INACTIVE->STARTING"
            )
            assert starting.cause == "STARTJOB event (control)"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_force_startjob_via_control_socket_tags_cause_and_started_by_control(
    short_root: Path,
) -> None:
    """(DL-68): sendevent FORCE_STARTJOB tags exactly like plain STARTJOB --
    "FORCE_STARTJOB event (control)" -- distinguishing the forced bypass
    (SEM-23: the condition below is false and would otherwise arm-and-wait)
    from an ordinary control-socket start in both the trace cause and the
    status verb's started_by."""
    text = (
        "insert_job: force_dep\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: force_job\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(force_dep)\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            resp = await _sendevent(server.path, "FORCE_STARTJOB", job="force_job")
            assert resp["ok"] is True

            async def done() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "force_job"})
                return r["jobs"]["force_job"]["status"] == "SUCCESS"

            await _wait_for_async(done)
            after = await _control_call(server.path, {"cmd": "status", "job": "force_job"})
            assert after["jobs"]["force_job"]["started_by"] == "FORCE_STARTJOB event (control)"
            starting = next(
                t
                for t in engine.oracle.trace()
                if t.job == "force_job" and t.transition == "INACTIVE->STARTING"
            )
            assert starting.cause == "FORCE_STARTJOB event (control)"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_trace_query_since_filtering(short_root: Path) -> None:
    text = "insert_job: tr_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter({("tr_job", 1): (0.05, 0)}, default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            resp = await _sendevent(server.path, "STARTJOB", job="tr_job")
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
            assert _body(none_resp, engine) == {
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

            resp = await _sendevent(server.path, "CHANGE_STATUS", job="ex_a", status="SUCCESS")
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
            assert _body(resp, engine) == {"ok": True, "waves": [["pl_a"], ["pl_b"], ["pl_c"]]}
        finally:
            await _teardown(engine, server, loop_task)

        engine2, server2, loop_task2 = await _serve(short_root / "run_cycle", cycle_text)
        try:
            resp2 = await _control_call(server2.path, {"cmd": "plan"})
            assert resp2["ok"] is False
        finally:
            await _teardown(engine2, server2, loop_task2)

    asyncio.run(scenario())


def test_spec_serves_the_rendered_block_and_nulls_without_texts(short_root: Path) -> None:
    """DL-64 `spec` verb: the job's preserve-rendered JIL block when the
    server was given spec texts (the `dsl41 run` path renders them from the
    post-placeholder AST), jil:null when it was not (embedders), and the
    usual unknown-job refusal."""
    text = (
        "insert_job: sp_box\njob_type: b\n\n"
        "insert_job: sp_a\njob_type: c\ncommand: echo hi\nmachine: m1\nbox_name: sp_box\n"
    )
    block = "insert_job: sp_a\njob_type: c\ncommand: echo hi\nmachine: m1\nbox_name: sp_box\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(
            short_root / "run", text, spec_texts={"sp_a": block}
        )
        try:
            resp = await _control_call(server.path, {"cmd": "spec", "job": "sp_a"})
            assert _body(resp, engine) == {
                "ok": True,
                "job": "sp_a",
                "job_type": "CMD",
                "box_name": "sp_box",
                "jil": block,
            }
            resp = await _control_call(server.path, {"cmd": "spec", "job": "sp_box"})
            assert resp["ok"] is True and resp["jil"] is None  # no text handed over
            resp = await _control_call(server.path, {"cmd": "spec", "job": "nope"})
            assert resp["ok"] is False
        finally:
            await _teardown(engine, server, loop_task)

        engine2, server2, loop_task2 = await _serve(short_root / "run_bare", text)
        try:
            resp = await _control_call(server2.path, {"cmd": "spec", "job": "sp_a"})
            assert resp["ok"] is True and resp["jil"] is None
        finally:
            await _teardown(engine2, server2, loop_task2)

    asyncio.run(scenario())


def test_spec_texts_helper_renders_post_placeholder_blocks() -> None:
    """The `dsl41 run` side of DL-64: _spec_texts maps every catalog job to
    its preserve-rendered statement block -- placeholder-resolved, comments
    kept, separating blank lines dropped."""
    from dsl41.ast_jil import parse
    from dsl41.cli import _spec_texts
    from dsl41.ir import lower_catalog
    from dsl41.placeholders import substitute

    source = (
        "/* the box */\n"
        "insert_job: st_box\njob_type: b\n\n\n"
        "insert_job: st_a\njob_type: c\n"
        "command: fakework ~{$WHO}~\nmachine: m1\nbox_name: st_box\n"
    )
    resolved, _ = substitute(source, {"WHO": "st_a"}, file="mem.jil")
    parsed = [parse(resolved, file="mem.jil")]
    catalog = lower_catalog(parsed, permit_unknown=False)
    texts = _spec_texts(parsed, catalog)
    assert set(texts) == {"st_box", "st_a"}
    assert texts["st_box"] == "/* the box */\ninsert_job: st_box\njob_type: b\n"
    assert texts["st_a"] == (
        "insert_job: st_a\njob_type: c\ncommand: fakework st_a\nmachine: m1\nbox_name: st_box\n"
    )


# ------------------------------------------------------------------ 3. subscribe


def test_subscribe_backfills_since_zero_then_streams_a_live_record_once(short_root: Path) -> None:
    text = "insert_job: sub_job\njob_type: c\ncommand: x\nmachine: m1\n"
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root, text)
        try:
            r1 = await _sendevent(server.path, "ON_HOLD", job="sub_job")
            assert r1["ok"] is True

            async def journaled() -> bool:
                records = read_journal(run_root / "journal.jsonl")
                return any(r.get("kind") == "ON_HOLD" for r in records)

            await _wait_for_async(journaled)

            reader, writer = await asyncio.open_unix_connection(str(server.path))
            try:
                writer.write(
                    json.dumps(_versioned({"cmd": "subscribe", "since": 0})).encode() + b"\n"
                )
                await writer.drain()
                ack = json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
                assert ack == {"ok": True, "subscribed": True}

                backfilled = []
                seen_opening = False
                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                    record = json.loads(line)
                    backfilled.append(record)
                    if record.get("rec") == "segment":
                        seen_opening = True
                    if record.get("kind") == "ON_HOLD":
                        break
                assert seen_opening
                assert sum(1 for r in backfilled if r.get("kind") == "ON_HOLD") == 1

                # a NEW live event, sent only after the backfill is fully drained
                r2 = await _sendevent(server.path, "OFF_HOLD", job="sub_job")
                assert r2["ok"] is True
                # every admitted input is now followed by its ss4 result
                # record, which streams like any other record -- drain past
                # the trailing ones to reach the new input
                live: dict[str, object] = {}
                while live.get("kind") is None:
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
    """DL-45 decision 1 regression, two engine bugs in one net.
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


def test_cli_rehearse_timezone_map_resolves_a_city_name(tmp_path: Path) -> None:
    """(SEM-35/DL-62): --timezone-map feeds the instance's autotimezone
    listing in; `timezone: Zurich` schedules at Zurich-local time (09:00
    CEST = 07:00 UTC) and the run completes without the city-default WARN."""
    jil = tmp_path / "estate.jil"
    jil.write_text(
        "insert_job: zrh_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\ntimezone: Zurich\n',
        encoding="utf-8",
    )
    tz_map = tmp_path / "ujo_timezones.txt"
    tz_map.write_text(
        "Entry Type Zone\n------ ---- ----\nZurich City Europe/Zurich\n", encoding="utf-8"
    )
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--timezone-map",
            str(tz_map),
            "--start",
            "2026-07-06T00:00:00",
            "--hours",
            "8",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "zrh_job" in result.output
    assert "SUCCESS" in result.output
    assert "WARN" not in result.output  # mapped, not assumed


def test_cli_rehearse_malformed_timezone_map_exits_2(tmp_path: Path) -> None:
    jil = tmp_path / "estate.jil"
    jil.write_text("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n", encoding="utf-8")
    tz_map = tmp_path / "bad_map.txt"
    tz_map.write_text("utterly not a listing at all\n", encoding="utf-8")
    result = cli_runner.invoke(app, ["rehearse", str(jil), "--timezone-map", str(tz_map)])
    assert result.exit_code == 2
    assert "--timezone-map" in result.output


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


def _query_cli(sock_path: Path, *args: str) -> subprocess.CompletedProcess:
    """One `dsl41 query ...` CLI invocation as a real subprocess, against a
    live control socket (the DL-65 is-success/is-failed predicates need the
    wire; CliRunner has no engine to talk to)."""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from dsl41.cli import app; app()",
            "query",
            *args,
            "--socket",
            str(sock_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_run_subprocess_sendevent_and_query_end_to_end(short_root: Path) -> None:
    """Integration test: spawn `dsl41 run` as a real subprocess (the pattern
    tests/test_runner_lifecycle.py uses), wait for its control socket, drive
    it with sendevent/query over the wire, then stop it with SIGTERM.
    DL-65 rides the same live window: the is-success/is-failed scriptable
    predicates are exercised on both branches (exit 0 on match, 1 on
    mismatch, current status on stdout either way)."""
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
            lambda: _sync_sendevent(sock_path, "STARTJOB", job="proc_job"),
            timeout_s=5.0,
        )
        assert resp["ok"] is True

        def succeeded() -> bool:
            r = _sync_control_call(sock_path, {"cmd": "status", "job": "proc_job"})
            return bool(r["ok"]) and r["jobs"]["proc_job"]["status"] == "SUCCESS"

        wait_for(succeeded, timeout_s=5.0)

        # the `dsl41 run` path hands the server real spec texts (DL-64):
        # the block served over the wire is the loaded source, byte-faithful
        spec = _sync_control_call(sock_path, {"cmd": "spec", "job": "proc_job"})
        assert spec["ok"] is True
        assert spec["jil"] == "insert_job: proc_job\njob_type: c\ncommand: exit 0\n"

        # DL-65 predicates against the SUCCESS job: is-success matches
        # (exit 0), is-failed does not (exit 1) -- both print the status
        matched = _query_cli(sock_path, "is-success", "--job", "proc_job")
        assert matched.returncode == 0, matched.stderr
        assert matched.stdout.strip() == "SUCCESS"
        mismatched = _query_cli(sock_path, "is-failed", "--job", "proc_job")
        assert mismatched.returncode == 1, mismatched.stderr
        assert mismatched.stdout.strip() == "SUCCESS"

        # flip the job to FAILURE over the wire; the predicates swap branches
        flip = _sync_sendevent(sock_path, "CHANGE_STATUS", job="proc_job", status="FAILURE")
        assert flip["ok"] is True

        def failed() -> bool:
            r = _sync_control_call(sock_path, {"cmd": "status", "job": "proc_job"})
            return bool(r["ok"]) and r["jobs"]["proc_job"]["status"] == "FAILURE"

        wait_for(failed, timeout_s=5.0)
        now_failed = _query_cli(sock_path, "is-failed", "--job", "proc_job")
        assert now_failed.returncode == 0, now_failed.stderr
        assert now_failed.stdout.strip() == "FAILURE"
        not_success = _query_cli(sock_path, "is-success", "--job", "proc_job")
        assert not_success.returncode == 1, not_success.stderr
        assert not_success.stdout.strip() == "FAILURE"

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
            resp = await _sendevent(server.path, "STARTJOB", job="pte_job")
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
            await _sendevent(server.path, "STARTJOB", job="ple_job")

            async def dispatched() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "ple_job"})
                return r["jobs"]["ple_job"]["status"] in ("RUNNING", "SUCCESS")

            await _wait_for_async(dispatched)
            resp = await _control_call(server.path, {"cmd": "status", "job": "ple_job"})
            jobs = resp["jobs"]["ple_job"]
            assert jobs["log_out"] == str(run_root / "logs" / "ple_job.1.out")
            assert jobs["log_err"] == str(run_root / "logs" / "ple_job.1.err")
            assert jobs["armed"] is False  # DL-54: the Q3 latch is on the wire
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
            await _sendevent(server.path, "STARTJOB", job="pls_job")

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
            await _sendevent(server.path, "STARTJOB", job="plb_box")

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


# ------------------------------- 8. deps/timers verbs + status additions (DL-65)


def test_deps_chain_globals_downstream_and_unknown_job_refused(short_root: Path) -> None:
    """DL-65 `deps` verb (the list-dependencies analog): upstream = the
    entities the job's own condition references (jobs vs globals split, the
    g: prefix stripped), downstream = the oracle's edge-trigger index
    (_referencers) -- who this job wakes. A chain a->b->c with a v(GFLAG)
    atom on b pins all three directions; a condition-free, unreferenced job
    reports three empty lists; an unknown job is refused."""
    text = (
        "insert_job: dp_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: dp_b\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(dp_a) & v(GFLAG) = go\n\n"
        "insert_job: dp_c\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(dp_b)\n\n"
        "insert_job: dp_d\njob_type: c\ncommand: w\nmachine: m1\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            mid = await _control_call(server.path, {"cmd": "deps", "job": "dp_b"})
            assert _body(mid, engine) == {
                "ok": True,
                "job": "dp_b",
                "upstream": ["dp_a"],
                "globals": ["GFLAG"],
                "downstream": ["dp_c"],
                "box_name": None,
                "members": [],
            }

            head = await _control_call(server.path, {"cmd": "deps", "job": "dp_a"})
            assert _body(head, engine) == {
                "ok": True,
                "job": "dp_a",
                "upstream": [],
                "globals": [],
                "downstream": ["dp_b"],
                "box_name": None,
                "members": [],
            }

            tail = await _control_call(server.path, {"cmd": "deps", "job": "dp_c"})
            assert _body(tail, engine) == {
                "ok": True,
                "job": "dp_c",
                "upstream": ["dp_b"],
                "globals": [],
                "downstream": [],
                "box_name": None,
                "members": [],
            }

            solo = await _control_call(server.path, {"cmd": "deps", "job": "dp_d"})
            assert _body(solo, engine) == {
                "ok": True,
                "job": "dp_d",
                "upstream": [],
                "globals": [],
                "downstream": [],
                "box_name": None,
                "members": [],
            }

            unknown = await _control_call(server.path, {"cmd": "deps", "job": "nope"})
            assert unknown["ok"] is False
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_deps_serves_box_containment_alongside_condition_edges(short_root: Path) -> None:
    """DL-65 review: condition edges alone are not a box's blast radius --
    KILLJOB/ON_HOLD on a box reaches every member with no condition edge in
    sight, so `deps` serves box_name (upward) and members (downward) too."""
    text = (
        "insert_job: bc_box\njob_type: b\n\n"
        "insert_job: bc_m1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bc_box\n\n"
        "insert_job: bc_m2\njob_type: c\ncommand: y\nmachine: m1\nbox_name: bc_box\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            box = await _control_call(server.path, {"cmd": "deps", "job": "bc_box"})
            assert box["ok"] is True
            assert box["members"] == ["bc_m1", "bc_m2"]
            assert box["box_name"] is None
            member = await _control_call(server.path, {"cmd": "deps", "job": "bc_m1"})
            assert member["box_name"] == "bc_box"
            assert member["members"] == []
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_change_status_on_declared_xinst_ghost_satisfies_a_cross_instance_atom(
    short_root: Path,
) -> None:
    """DL-65 review MAJOR: "JOB^INST" with INST a declared insert_xinst is a
    legal CHANGE_STATUS target (SEM-07) -- the store pseudo-entity is created
    on demand and the dependent's cross-instance atom fires. An undeclared
    instance suffix stays refused."""
    text = (
        "insert_xinst: PRD\nxtype: a\n\n"
        "insert_job: xg_dep\njob_type: c\ncommand: x\nmachine: m1\n"
        "condition: s(FEED^PRD, 9999)\n"
    )

    async def scenario() -> None:
        adapter = FakeAdapter(default=(0.05, 0))
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            refused = await _sendevent(
                server.path,
                "CHANGE_STATUS",
                job="FEED^NOPE",
                status="SUCCESS",
            )
            assert refused["ok"] is False

            resp = await _sendevent(
                server.path,
                "CHANGE_STATUS",
                job="FEED^PRD",
                status="SUCCESS",
            )
            assert resp["ok"] is True

            async def dep_fired() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "xg_dep"})
                return r["ok"] and r["jobs"]["xg_dep"]["status"] == "SUCCESS"

            await _wait_for_async(dep_fired)

            # the ghost is now a store-only row: visible in status, nulls for
            # its catalog placement (the DL-65 status additions)
            ghost = await _control_call(server.path, {"cmd": "status", "job": "FEED^PRD"})
            assert ghost["ok"] is True
            row = ghost["jobs"]["FEED^PRD"]
            assert row["status"] == "SUCCESS"
            assert row["job_type"] is None and row["box_name"] is None
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_timers_oracle_deadlines_due_ordered_no_schedule_entries_without_scheduler(
    short_root: Path,
) -> None:
    """DL-65 `timers` verb, oracle half: two jobs held RUNNING by an inert
    adapter each arm a term_run_time deadline (1 and 2 minutes out); the
    response carries exactly {due, job, kind} per entry, due-ordered. With
    no scheduler wired in, no kind:"schedule" entry exists at all -- the
    section is simply absent, not an error."""
    text = (
        "insert_job: tm_one\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 1\n\n"
        "insert_job: tm_two\njob_type: c\ncommand: y\nmachine: m1\nterm_run_time: 2\n"
    )

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # unscripted: park RUNNING forever
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            for job in ("tm_one", "tm_two"):
                resp = await _sendevent(server.path, "STARTJOB", job=job)
                assert resp["ok"] is True

            async def both_running() -> bool:
                r = await _control_call(server.path, {"cmd": "status"})
                return all(r["jobs"][j]["status"] == "RUNNING" for j in ("tm_one", "tm_two"))

            await _wait_for_async(both_running)
            resp = await _control_call(server.path, {"cmd": "timers"})
            assert resp["ok"] is True
            entries = resp["timers"]
            assert [set(e) for e in entries] == [{"due", "job", "kind"}] * 2
            assert [e["job"] for e in entries] == ["tm_one", "tm_two"]  # 1min before 2min
            assert [e["kind"] for e in entries] == ["term_run_time", "term_run_time"]
            assert entries[0]["due"] < entries[1]["due"]  # ISO strings order chronologically
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_timers_merges_scheduler_next_ticks_due_ordered(short_root: Path) -> None:
    """DL-65 `timers` verb, scheduler half: with a Scheduler wired in, each
    scheduled job's NEXT calendar tick (Scheduler.upcoming()) joins the
    oracle deadlines as kind:"schedule", one due-ordered list. A parked
    term_run_time deadline (~1 minute out) sorts before a start_times tick
    ~2 hours out; the schedule entry's due is the exact tick instant."""
    now = RealClock().now()
    tick = (now + timedelta(hours=2)).replace(second=0, microsecond=0)
    text = (
        "insert_job: tm_run\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 1\n\n"
        "insert_job: sch_job\njob_type: c\ncommand: y\nmachine: m1\n"
        f'date_conditions: 1\ndays_of_week: all\nstart_times: "{tick:%H:%M}"\n'
    )
    scheduler = Scheduler(lower_source(text), start=now)
    assert scheduler.upcoming() == [(tick, "sch_job")]  # the new snapshot API itself

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)
        engine, server, loop_task = await _serve(
            short_root / "run", text, adapter=adapter, scheduler=scheduler
        )
        try:
            resp = await _sendevent(server.path, "STARTJOB", job="tm_run")
            assert resp["ok"] is True

            async def running() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "tm_run"})
                return r["jobs"]["tm_run"]["status"] == "RUNNING"

            await _wait_for_async(running)
            resp = await _control_call(server.path, {"cmd": "timers"})
            assert resp["ok"] is True
            entries = resp["timers"]
            assert [(e["job"], e["kind"]) for e in entries] == [
                ("tm_run", "term_run_time"),
                ("sch_job", "schedule"),
            ]
            assert entries[1]["due"] == tick.isoformat()
            assert entries[0]["due"] < entries[1]["due"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_timers_synthesizes_a_filewatch_row_and_status_gains_watching_for_live_fw(
    short_root: Path,
) -> None:
    """DL-68 filewatch visibility: a live FW run (an in-flight adapter task,
    nothing else) gets one synthesized due-less timers row after the dated
    ones -- {due: null, kind: "filewatch", detail: "watching <file> every
    <N>s, min_size <n>"} -- and its status payload gains a "watching" dict.
    The parked CMD job proves the key is FW-only, and before STARTJOB the FW
    job has neither row nor key (not live = not watching)."""
    text = (
        "insert_job: fwv_cmd\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 1\n\n"
        "insert_job: fwv_fw\njob_type: f\nwatch_file: /nonexistent/fwv.dat\n"
        "watch_interval: 5\nwatch_file_min_size: 12\nmachine: m1\n"
    )

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # parks both jobs live forever
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            before = await _control_call(server.path, {"cmd": "status"})
            assert "watching" not in before["jobs"]["fwv_fw"]
            resp = await _control_call(server.path, {"cmd": "timers"})
            assert resp["timers"] == []

            for job in ("fwv_cmd", "fwv_fw"):
                resp = await _sendevent(server.path, "STARTJOB", job=job)
                assert resp["ok"] is True

            async def both_running() -> bool:
                r = await _control_call(server.path, {"cmd": "status"})
                return all(r["jobs"][j]["status"] == "RUNNING" for j in ("fwv_cmd", "fwv_fw"))

            await _wait_for_async(both_running)
            resp = await _control_call(server.path, {"cmd": "timers"})
            assert resp["ok"] is True
            entries = resp["timers"]
            assert [(e["job"], e["kind"]) for e in entries] == [
                ("fwv_cmd", "term_run_time"),  # dated rows first
                ("fwv_fw", "filewatch"),
            ]
            fw_row = entries[1]
            assert fw_row["due"] is None
            # path-first, no "watching" prefix: kind already says filewatch
            # and the redundant word pushed the path off narrow panes
            assert fw_row["detail"] == "/nonexistent/fwv.dat every 5s, min_size 12"

            status = await _control_call(server.path, {"cmd": "status"})
            assert status["jobs"]["fwv_fw"]["watching"] == {
                "file": "/nonexistent/fwv.dat",
                "interval": 5,
                "min_size": 12,
            }
            assert "watching" not in status["jobs"]["fwv_cmd"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_timers_and_status_drop_the_filewatch_row_once_the_fw_run_completes(
    short_root: Path,
) -> None:
    """DL-68 filewatch visibility, completion side: a watch is live ONLY
    while the adapter task is in-flight (`Engine._live`) -- once the FW job
    finishes, both the synthesized `timers` row and status's "watching" key
    must be gone, not just stale. Distinguishes "gone because completed"
    from the earlier test's "absent because never started"."""
    text = (
        "insert_job: fwc_fw\njob_type: f\nwatch_file: /nonexistent/fwc.dat\n"
        "watch_interval: 5\nmachine: m1\n"
    )

    async def scenario() -> None:
        adapter = FakeAdapter({("fwc_fw", 1): (0.05, 0)})  # completes fast, then quiet
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            resp = await _sendevent(server.path, "STARTJOB", job="fwc_fw")
            assert resp["ok"] is True

            async def running() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "fwc_fw"})
                return r["jobs"]["fwc_fw"]["status"] == "RUNNING"

            await _wait_for_async(running)
            mid = await _control_call(server.path, {"cmd": "timers"})
            assert [e["kind"] for e in mid["timers"]] == ["filewatch"]
            mid_status = await _control_call(server.path, {"cmd": "status", "job": "fwc_fw"})
            assert "watching" in mid_status["jobs"]["fwc_fw"]

            async def done() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "fwc_fw"})
                return r["jobs"]["fwc_fw"]["status"] == "SUCCESS"

            await _wait_for_async(done)
            after_timers = await _control_call(server.path, {"cmd": "timers"})
            assert after_timers["timers"] == []
            after_status = await _control_call(server.path, {"cmd": "status", "job": "fwc_fw"})
            assert "watching" not in after_status["jobs"]["fwc_fw"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_status_job_type_and_box_name_for_members_boxes_and_store_ghosts(
    short_root: Path,
) -> None:
    """DL-65 status additions: catalog placement rides every status row --
    a member reports its job_type and owning box, the box itself reports
    job_type BOX with box_name null. A store-only ghost (a STATUS event for
    a name outside the catalog -- CHANGE_STATUS's injection parity -- creates
    the JobRuntime lazily) has no catalog row at all: job_type and box_name
    are both null, yet the single-job status query still resolves it."""
    text = (
        "insert_job: bx_box\njob_type: b\n\n"
        "insert_job: bx_mem\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx_box\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            resp = await _control_call(server.path, {"cmd": "status"})
            assert resp["jobs"]["bx_mem"]["job_type"] == "CMD"
            assert resp["jobs"]["bx_mem"]["box_name"] == "bx_box"
            assert resp["jobs"]["bx_box"]["job_type"] == "BOX"
            assert resp["jobs"]["bx_box"]["box_name"] is None

            engine.inject(
                Event(
                    at=engine.clock.now(),
                    kind="STATUS",
                    payload={"job": "gh_ghost", "status": "SUCCESS"},
                )
            )

            async def ghost_visible() -> bool:
                r = await _control_call(server.path, {"cmd": "status"})
                return "gh_ghost" in r["jobs"]

            await _wait_for_async(ghost_visible)
            single = await _control_call(server.path, {"cmd": "status", "job": "gh_ghost"})
            assert single["ok"] is True
            ghost = single["jobs"]["gh_ghost"]
            assert ghost["status"] == "SUCCESS"
            assert ghost["job_type"] is None
            assert ghost["box_name"] is None
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_status_spec_drift_null_without_fingerprint(short_root: Path) -> None:
    """DL-65 spec_drift, the embedder shape: a server started without an
    estate fingerprint has nothing to check against -- the flag is null,
    never false (unknown is not 'clean')."""
    text = "insert_job: nf_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            resp = await _control_call(server.path, {"cmd": "status"})
            assert resp["ok"] is True
            assert resp["spec_drift"] is None
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_status_spec_drift_false_then_true_on_rewrite_with_lazy_interval(
    short_root: Path,
) -> None:
    """DL-65 spec_drift, the `dsl41 run` shape: with a matching sha256
    fingerprint the flag is false; rewriting the file flips it to true only
    once the DRIFT_CHECK_INTERVAL_S lazy re-check runs (forced here by
    clearing _drift_checked_at, the test seam for the 15s interval). The
    cache cuts both ways: restoring the original bytes WITHOUT forcing a
    re-check still reports true -- the flag is a sampled hint, not a live
    watch."""
    text = "insert_job: fp_job\njob_type: c\ncommand: x\nmachine: m1\n"
    estate = short_root / "estate.jil"
    estate.write_text(text, encoding="utf-8")
    fingerprint = {str(estate): hashlib.sha256(estate.read_bytes()).hexdigest()}

    async def scenario() -> None:
        engine, server, loop_task = await _serve(
            short_root / "run", text, estate_fingerprint=fingerprint
        )
        try:
            clean = await _control_call(server.path, {"cmd": "status"})
            assert clean["spec_drift"] is False

            estate.write_text(text + "/* touched */\n", encoding="utf-8")
            cached = await _control_call(server.path, {"cmd": "status"})
            assert cached["spec_drift"] is False  # within the 15s interval: stale-clean

            server._drift_checked_at = None  # force the lazy re-check
            drifted = await _control_call(server.path, {"cmd": "status"})
            assert drifted["spec_drift"] is True

            estate.write_text(text, encoding="utf-8")  # restore, but do NOT force
            still = await _control_call(server.path, {"cmd": "status"})
            assert still["spec_drift"] is True
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_cli_query_predicates_require_job_and_unknown_verb_lists_them() -> None:
    """DL-65 CLI arg validation, in-process (both paths exit before touching
    the socket): a predicate without --job exits 2; an unknown verb's error
    listing now includes the new verbs and predicates."""
    result = cli_runner.invoke(app, ["query", "is-success", "--socket", "/nonexistent.sock"])
    assert result.exit_code == 2
    assert "--job" in result.output

    result = cli_runner.invoke(app, ["query", "bogus", "--socket", "/nonexistent.sock"])
    assert result.exit_code == 2
    assert "deps" in result.output
    assert "timers" in result.output
    assert "is-success" in result.output
    assert "is-failed" in result.output


def test_cli_brief_flags_include_the_armed_latch_in_ihna_order() -> None:
    """DL-68 review: `query status --brief` and the TUI jobs table render
    the same flags column from the same status payload -- the armed latch
    (SEM-32) must show as A on the headless skim too, after I/H/N."""
    from dsl41.cli import _brief_flags

    all_on = {"on_ice": True, "on_hold": True, "on_noexec": True, "armed": True}
    assert _brief_flags(all_on) == "IHNA"
    assert _brief_flags({"armed": True}) == "A"
    assert _brief_flags({"status": "INACTIVE"}) == ""


# ---------------------------------------- revision-bearing reads (DL-87, ss6)


def test_global_read_answers_named_absence_and_presence_with_revisions(
    short_root: Path,
) -> None:
    """concurrency-model ss6: `global` / `globals` answer NAMED entities with
    {present, value, state_rev} and insert nothing.

    The naming is the whole point. A map of the globals that happen to exist
    cannot express the absence a conditional create has to condition on, so an
    unset name comes back present:false at revision 0 rather than missing --
    and 0 means absent unambiguously, because the catalog seed is itself an
    input and anything that exists is at 1 or more."""
    text = (
        "insert_global: DECLARED\nvalue: go\n\n"
        "insert_job: gj\njob_type: c\ncommand: x\nmachine: m1\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            resp = await _control_call(server.path, {"cmd": "global", "name": "DECLARED"})
            assert resp["ok"] is True
            assert resp["globals"] == {"DECLARED": {"present": True, "value": "go", "state_rev": 1}}

            resp = await _control_call(server.path, {"cmd": "global", "name": "NEVER_SET"})
            assert resp["globals"]["NEVER_SET"] == {
                "present": False,
                "value": None,
                "state_rev": 0,
            }

            # the read INSERTED nothing -- asking about a name must not create it
            assert "NEVER_SET" not in engine.oracle.store.globals_

            resp = await _sendevent(server.path, "SET_GLOBAL", name="NEVER_SET", value="now")
            assert resp["ok"] is True

            async def landed() -> bool:
                r = await _control_call(server.path, {"cmd": "global", "name": "NEVER_SET"})
                return bool(r["globals"]["NEVER_SET"]["present"])

            await _wait_for_async(landed)
            resp = await _control_call(
                server.path, {"cmd": "globals", "names": ["DECLARED", "NEVER_SET", "STILL_NOT"]}
            )
            assert resp["globals"] == {
                "DECLARED": {"present": True, "value": "go", "state_rev": 1},
                "NEVER_SET": {"present": True, "value": "now", "state_rev": 1},
                "STILL_NOT": {"present": False, "value": None, "state_rev": 0},
            }
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_globals_read_refuses_a_malformed_request(short_root: Path) -> None:
    """`globals` needs a list and every name must be a string: a silent
    coercion here would hand a client a confident answer about an entity it
    never asked for."""
    text = "insert_job: gj2\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            resp = await _control_call(server.path, {"cmd": "globals", "names": "DECLARED"})
            assert resp["ok"] is False and "list of names" in resp["error"]
            resp = await _control_call(server.path, {"cmd": "globals", "names": [7]})
            assert resp["ok"] is False and "must be a string" in resp["error"]
            resp = await _control_call(server.path, {"cmd": "global"})
            assert resp["ok"] is False and "must be a string" in resp["error"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_status_publishes_the_revision_a_precondition_would_name(short_root: Path) -> None:
    """DL-87: a client that acts on what it saw must be able to say what it
    saw. The revision moves once per input that changed the job -- the whole
    STARTJOB cascade here is one input, not one per transition."""
    text = "insert_job: rev_j\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # park RUNNING: one input, then quiet
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            resp = await _control_call(server.path, {"cmd": "status", "job": "rev_j"})
            before = resp["jobs"]["rev_j"]["state_rev"]

            resp = await _sendevent(server.path, "STARTJOB", job="rev_j")
            assert resp["ok"] is True

            async def running() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "rev_j"})
                return r["jobs"]["rev_j"]["status"] == "RUNNING"

            await _wait_for_async(running)
            resp = await _control_call(server.path, {"cmd": "status", "job": "rev_j"})
            # INACTIVE -> STARTING -> RUNNING plus the run_number bump, all one
            # input: exactly one revision, or `expect` is unusable
            assert resp["jobs"]["rev_j"]["state_rev"] == before + 1
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------------------ a lost round trip has two readings (DL-92, S7c)


def test_a_request_that_never_left_is_not_the_same_failure_as_one_with_no_answer(
    short_root: Path,
) -> None:
    """`roundtrip` raises one exception type for two facts, and the CLI turns
    the difference into two exit codes (2 refused / 4 unknown), so the split
    has to be made where the transport is -- at the write, not at the caller.

    The distinction is not academic: the engine fsyncs an attempt BEFORE it
    feeds it, so a connection that dies after the write may well have died
    over a command that is already durably admitted. Reading that as a
    refusal tells an operator to send it again, which is how it applies
    twice."""
    from dsl41.runner_control import ControlClientError, roundtrip

    missing = short_root / "nobody.sock"
    with pytest.raises(ControlClientError) as never:
        roundtrip(missing, {"cmd": "status"})
    assert never.value.delivered is False  # nothing was written anywhere

    path = short_root / "hangup.sock"
    server = socket_mod.socket(socket_mod.AF_UNIX)
    server.bind(str(path))
    server.listen(1)

    def hang_up() -> None:
        conn, _ = server.accept()
        conn.recv(65536)  # take the request, answer nothing
        conn.close()

    thread = threading.Thread(target=hang_up)
    thread.start()
    try:
        with pytest.raises(ControlClientError) as lost:
            roundtrip(path, {"cmd": "status"})
        # an empty read is not an OSError: it falls out of the loop and lands
        # on json.loads(b""), which is why the delivered arm catches ValueError
        # as well and why this test exists rather than a mutation of the flag
        assert lost.value.delivered is True
    finally:
        thread.join(timeout=5)
        server.close()


def test_a_reply_that_is_not_an_object_was_still_delivered(short_root: Path) -> None:
    """The last arm: the engine answered, and answered something this client
    cannot read. The command reached it, so the answer to "did it apply" is
    still "no idea"."""
    from dsl41.runner_control import ControlClientError, roundtrip

    path = short_root / "notobject.sock"
    server = socket_mod.socket(socket_mod.AF_UNIX)
    server.bind(str(path))
    server.listen(1)

    def answer_a_list() -> None:
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall(b"[1, 2, 3]\n")
        conn.close()

    thread = threading.Thread(target=answer_a_list)
    thread.start()
    try:
        with pytest.raises(ControlClientError) as lost:
            roundtrip(path, {"cmd": "status"})
        assert lost.value.delivered is True
        assert "not a JSON object" in str(lost.value)
    finally:
        thread.join(timeout=5)
        server.close()


def test_the_cli_maps_every_mutation_outcome_including_the_two_transport_ones(
    monkeypatch, capsys
) -> None:
    """DL-92's exit contract, whole, at the one helper both mutating verbs go
    through. Four codes for four answers, and -- since S7c -- two readings of
    NO answer, because a request that never left and one that left and was
    not answered are different facts about the same silence.

    Table-driven so that adding a fifth outcome without a code fails here,
    rather than defaulting to 0 in front of an operator."""
    import dsl41.runner_control as control_mod
    from dsl41.cli import _mutate
    from dsl41.runner_control import ControlClientError

    request = {"cmd": "sendevent", "request_id": "req-7"}
    cases: list[tuple[object, int, bool]] = [
        ({"ok": True, "index": 3}, 0, False),
        ({"ok": False, "refused": True, "error": "no expect"}, 2, False),
        ({"ok": False, "decision": "rejected", "index": 4}, 3, False),
        ({"ok": False, "error": "no decision within 5.0s"}, 4, True),
        (ControlClientError("no such file"), 2, False),
        (ControlClientError("engine hung up", delivered=True), 4, True),
    ]
    for answer, code, names_the_id in cases:

        def fake_roundtrip(_path, _request, *, answer=answer, **_kw):
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(control_mod, "roundtrip", fake_roundtrip)
        with pytest.raises(typer.Exit) as exit_info:
            _mutate(Path("/nowhere.sock"), request)
        assert exit_info.value.exit_code == code, answer
        err = capsys.readouterr().err
        # the id is the only thing that makes a retry safe, so every outcome
        # that may still apply has to carry it and no other outcome may
        assert ("--request-id req-7" in err) is names_the_id, answer
