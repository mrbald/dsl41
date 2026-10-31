"""The one-line `decision` record, the `dispatch` record after it, and control protocol v3
(DL-118).

Normative spec: `docs/runner-design.md` ss7 (journal before effect); `docs/control-protocol.md`
ss2 and ss5 (the version handshake, and what the subscribe stream promises about which records).

The properties, and the contrast that makes each non-vacuous:

  * **Journal before effect.** Killed at every store append boundary, the store never holds a
    `dispatch` record whose decision is missing, and a successor dispatches each decided start
    exactly once across both incarnations. The contrast is the boundary either side of the
    admission's transaction: before it nothing of the input exists; after the dispatch records
    the successor dispatches nothing again.
  * **Identity at birth.** A `dispatch` record names a uuid4 run_id, and an attempt's spool
    evidence must name it.
  * **The stream and the wire.** `decision` crosses the backfill/live seam exactly once, as
    every record does, and a v2 request is refused naming v3.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_runner_control import (
    _sendevent,
    _serve,
    _teardown,
    _versioned,
    _wait_for_async,
)

from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    PROTOCOL_VERSION,
    EnvelopeError,
    parse_envelope,
)
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_journal import Journal, dispatch_record, dispatched_runs, read_journal
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_store import MemoryStore

T0 = datetime(2026, 7, 1, 8, 0)

_SOLO_JIL = "insert_job: j\njob_type: c\ncommand: x\n"

#: a fixed run_id in the ss11a uuid4 grammar -- the writer and reader refuse
#: freehand strings, so fixtures that pass through either must speak it
_RID = "00000000-0000-4000-8000-000000000001"

#: two members under one box, so ONE admitted input plans TWO starts: the window between the
#: decision and a start AND between one start and the next.
_BOX_JIL = (
    "insert_job: nightly\njob_type: b\n\n"
    "insert_job: a\njob_type: c\ncommand: x\nbox_name: nightly\n\n"
    "insert_job: b\njob_type: c\ncommand: x\nbox_name: nightly\n"
)


def _records(run_root: Path) -> list[dict[str, Any]]:
    """Every record of the store, as the wire projects it."""
    return read_journal(run_root)


def _kinds(run_root: Path) -> list[str]:
    """Every record's store kind, in seq order."""
    from dsl41.runner_journal import read_records

    return [record.kind for _, record in read_records(run_root)]


def _dispatches(records: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return [(r["job"], r["run_number"]) for r in records if r.get("rec") == "dispatch"]


def _decided(records: list[dict[str, Any]]) -> set[int]:
    return {r["index"] for r in records if r.get("rec") == "decision"}


# --------------------------------------------- 1. journal before effect (ss7)


class _Killed(Exception):
    """The engine's process, gone at a journal append boundary."""


def _run_dying_at(run_root: Path, append: int | None) -> int:
    """Start one box through a fresh engine, dying just before the `append`-th store append
    AFTER genesis (None runs it to quiescence). Answers how many records genesis itself wrote.

    `Journal.append` is the store's only durability unit, one transaction, so a process that
    dies at a boundary leaves exactly the records before it and nothing after. The fault is
    injected in FRONT of the append, so the records it would have made do not exist."""
    engine = start_run(
        lower_source(_BOX_JIL),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    journal = engine.journal
    assert journal is not None
    genesis = journal.last_seq
    appends = 0
    original = journal.append

    def counted(*records: Any) -> int:
        nonlocal appends
        appends += 1
        if appends == append:
            raise _Killed(f"engine gone before store append {appends}")
        return original(*records)

    journal.append = counted  # type: ignore[method-assign]

    async def scenario() -> None:
        try:
            engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "nightly"}))
            await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        except _Killed:
            pass
        finally:
            await engine.shutdown()  # in the loop the adapter tasks belong to

    try:
        asyncio.run(scenario())
    finally:
        journal.append = original  # type: ignore[method-assign]
        journal.close()
    return genesis


async def _resume(run_root: Path):
    return await resume_run(
        lower_source(_BOX_JIL),
        run_root,
        clock=VirtualClock(start=T0 + timedelta(minutes=2)),
        adapters={"CMD": FakeAdapter(default=None)},
        settle_seconds=0.0,
        grace_seconds=0.0,
    )


def _take_over(run_root: Path) -> None:
    """A successor engine resumes the root, runs its loop, which dispatches the starts the
    store decided and never dispatched, and stops."""

    async def scenario() -> None:
        engine = await _resume(run_root)
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())


def test_pr35_a_crash_at_any_append_dispatches_each_start_once(tmp_path: Path) -> None:
    """Journal before effect: the engine is killed at EVERY store append boundary, and every
    resulting store, and its successor's, is checked.

    In the crash's store a `dispatch` record never precedes the decision of the input that
    planned it, and an input never stands without its decision. A successor dispatches each
    decided start across both incarnations: a start the crash took before its `dispatch`
    record is dispatched by the successor, and one after it, its attempt lost with the engine,
    is run again once as the run's next attempt (DL-424), never a third time."""
    genesis = _run_dying_at(tmp_path / "intact", None)
    intact = _kinds(tmp_path / "intact")
    planned = _dispatches(_records(tmp_path / "intact"))
    assert planned == [("a", 1), ("b", 1)]  # one decision, two starts
    decision_at = intact.index("decision")
    assert intact[decision_at - 1 : decision_at + 3] == [
        "input",
        "decision",
        "dispatch",
        "dispatch",
    ]

    appends = 0
    for append in range(1, len(intact) - genesis + 1):
        run_root = tmp_path / f"crash{append}"
        assert _run_dying_at(run_root, append) == genesis
        kinds = _kinds(run_root)
        if len(kinds) == len(intact):
            break  # past the last append: nothing more to kill
        appends += 1
        assert kinds == intact[: len(kinds)]
        records = _records(run_root)
        if _dispatches(records):
            assert _decided(records), "a dispatch with no decision before it"
        attempts = {r["seq"] for r in records if r.get("rec") == "input"}
        assert attempts <= _decided(records)

        _take_over(run_root)
        successor = _records(run_root)
        assert successor[: len(records)] == records
        if "decision" in kinds:  # the input was admitted
            # each planned run is dispatched; an attempt the crash left with no record and no
            # process is run again once, as the next attempt of the same run (DL-424)
            lost = _dispatches(records)
            assert sorted(set(_dispatches(successor))) == planned
            assert sorted(_dispatches(successor)) == sorted(planned + lost)
    assert appends >= 3  # the admission, then each of the two dispatches


# ------------------------------------------- 3a. identity at birth and readback


# ------------------------------------------- 3. identity at birth and readback


def test_a_dispatch_record_refuses_a_run_id_outside_the_grammar() -> None:
    """A run_id is the ss11a uuid4 string, and the writer refuses any other: an empty or
    freehand string would survive a presence check and then lose to a fallback mint downstream
    (DL-118)."""
    journal = Journal(MemoryStore())
    for bad in ("", "not-a-uuid", "00000000-0000-1000-8000-000000000001"):
        with pytest.raises(EngineError, match="grammar"):
            journal.append(dispatch_record("j", 1, attempt=bad, spool=None, started_at=T0, rev=1))
    journal.append(dispatch_record("j", 1, attempt=_RID, spool=None, started_at=T0, rev=1))
    assert [r["rec"] for r in read_journal(journal.store)] == ["dispatch"]


def test_the_dispatch_reader_refuses_a_bad_id_and_names_each_run_s_latest_attempt() -> None:
    """The read-side half: this code never writes a dispatch whose run_id is outside the
    grammar, so meeting one is corruption or a foreign writer and the reader refuses rather
    than carry it (DL-118). A run dispatched again (a replacement attempt after an
    interruption, DL-424) is bound to its latest attempt, the identity its current spool
    names."""
    other = "00000000-0000-4000-8000-000000000002"

    def dispatch(run_id: object) -> dict[str, Any]:
        return {"rec": "dispatch", "job": "j", "run_number": 1, "run_id": run_id}

    assert dispatched_runs([dispatch(_RID), dispatch(_RID)]) == {("j", 1): _RID}
    assert dispatched_runs([dispatch(_RID), dispatch(other)]) == {("j", 1): other}
    for bad in ("not-a-uuid", None, "", "00000000-0000-1000-8000-000000000001"):
        with pytest.raises(EngineError, match="ss11a"):
            dispatched_runs([dispatch(bad)])


def test_pr36a_a_restart_refuses_a_split_and_appends_nothing(tmp_path: Path) -> None:
    """The whole restart, against a real store whose own `dispatch` record bound the attempt's
    id. A spool record naming a stranger refuses the restart, and the refusal appends NOTHING
    to the store: the identity preflight runs before step 5 writes."""
    run_root = tmp_path / "run"
    engine = start_run(
        lower_source(_SOLO_JIL),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    before = _kinds(run_root)
    [run_id] = dispatched_runs(_records(run_root)).values()
    run_dir = run_root / "runs" / "j.1" / "1"
    run_dir.mkdir(parents=True)
    (run_dir / "spawn.json").write_text(json.dumps({"run_id": "rid-else"}))
    with pytest.raises(EngineError, match=f"'rid-else'.*{run_id!r}"):
        asyncio.run(
            resume_run(
                lower_source(_SOLO_JIL),
                run_root,
                clock=VirtualClock(start=T0),
                adapters={"CMD": FakeAdapter(default=None)},
            )
        )
    assert _kinds(run_root) == before


# ------------------------------------------------- 4. the stream and the wire


def test_subscribe_forwards_decision_once_across_the_seam(short_root: Path) -> None:
    """PR-49's `decision` half, and control-protocol ss5.

    `decision` carries its attempt's number under `index`, never `seq` (DL-89). The seam is
    the store's last seq, read as the feed joins the fan-out, so every record, a decision
    included, crosses it exactly once. The stream is checked on both sides of the seam: a
    decision arrives in the backfill, and a decision for an input admitted afterwards arrives
    live.
    """
    text = "insert_job: sub_job\njob_type: c\ncommand: x\nmachine: m1\n"
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root, text)
        try:
            assert (await _sendevent(server.path, "ON_HOLD", job="sub_job"))["ok"] is True

            async def decided() -> bool:
                return any(r.get("rec") == "decision" for r in read_journal(run_root))

            await _wait_for_async(decided)

            reader, writer = await asyncio.open_unix_connection(str(server.path))
            try:
                writer.write(
                    json.dumps(_versioned({"cmd": "subscribe", "since": 0})).encode() + b"\n"
                )
                await writer.drain()
                ack = json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
                assert ack == {"ok": True, "subscribed": True, "since": 0}

                backfilled: list[dict[str, Any]] = []
                while not any(r.get("kind") == "ON_HOLD" for r in backfilled):
                    backfilled.append(
                        json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
                    )
                held = next(r for r in backfilled if r.get("kind") == "ON_HOLD")
                # its decision follows on the stream, unsequenced. Drained
                # rather than read as the very next line: the append order
                # after an admission is the engine's business, and a test
                # that pinned it would be timing, not protocol.
                while not any(r.get("rec") == "decision" for r in backfilled):
                    backfilled.append(
                        json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
                    )
                [decision] = [r for r in backfilled if r["rec"] == "decision"]
                assert decision["index"] == held["seq"] and "seq" not in decision
                assert decision["decision"] == "applied"

                # a NEW input, admitted only after the backfill is drained
                assert (await _sendevent(server.path, "OFF_HOLD", job="sub_job"))["ok"] is True
                live: list[dict[str, Any]] = []
                while not any(r.get("rec") == "decision" for r in live):
                    live.append(json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0)))
                released = next(r for r in live if r.get("kind") == "OFF_HOLD")
                [live_decision] = [r for r in live if r["rec"] == "decision"]
                assert live_decision["index"] == released["seq"] and "seq" not in live_decision
                # each record crossed the seam exactly once
                assert sum(1 for r in backfilled if r.get("kind") == "OFF_HOLD") == 0
                assert sum(1 for r in live if r.get("kind") == "OFF_HOLD") == 1
                assert sum(1 for r in live if r.get("rec") == "decision") == 1
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_cli_query_subscribe_names_the_version_and_streams(short_root: Path) -> None:
    """`dsl41 query subscribe` opens a raw socket of its own rather than going
    through `roundtrip`, and from v2 to v3 it sent no `v` at all: the server
    refused it, the refusal does not close the connection, and the command
    printed one refusal line and hung. Run as a real subprocess against a live
    server, the first line out must be the ack and a decision must follow --
    which is the only place the CLI's stamp site is exercised."""
    text = "insert_job: sub_job\njob_type: c\ncommand: x\nmachine: m1\n"
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root, text)
        try:
            assert (await _sendevent(server.path, "ON_HOLD", job="sub_job"))["ok"] is True
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "dsl41",
                "query",
                "subscribe",
                "--socket",
                str(server.path),
                "--since",
                "0",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdout is not None
            try:
                first = json.loads(await asyncio.wait_for(proc.stdout.readline(), timeout=20.0))
                assert first == {"ok": True, "subscribed": True, "since": 0}, first
                seen: list[dict[str, Any]] = []
                while not any(r.get("rec") == "decision" for r in seen):
                    seen.append(
                        json.loads(await asyncio.wait_for(proc.stdout.readline(), timeout=20.0))
                    )
            finally:
                proc.terminate()
                with contextlib.suppress(ProcessLookupError):
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_v2_request_is_refused_naming_v3() -> None:
    """control-protocol ss2, DL-118 on DL-90's precedent: v2 is gone, not
    deprecated. The subscribe stream's records changed shape, so a v2 client
    waiting on `rec == "effect"` would silently stop seeing intents -- which
    is not an additive change, and a compatibility projection would be a
    second record shape for one fact.

    The refusal NAMES the version this engine speaks, because a client told
    only "refused" has nothing to migrate to."""
    assert PROTOCOL_VERSION == 3
    request = {
        "v": 2,
        "baseline_id": "the-log",
        "request_id": "r1",
        "verb": "ON_HOLD",
        "payload": {"job": "j"},
        "expect": {"job:j": 1},
    }
    with pytest.raises(EnvelopeError, match="this engine speaks v3"):
        parse_envelope(request, addressed="job:j", baseline_id="the-log")
    parsed = parse_envelope({**request, "v": 3}, addressed="job:j", baseline_id="the-log")
    assert parsed.request_id == "r1"


def test_an_unknown_field_on_a_control_request_is_ignored() -> None:
    """The OTHER half of the control socket's row in the evolution matrix
    (docs/protocol-evolution.md ss7, cases 3 and 4), and a SEPARATE test on
    purpose: an unknown FIELD follows the row's tolerance rule while an
    unsupported VERSION always refuses, and one message that is both proves
    neither.

    The control socket is a TOLERANT row (control-protocol ss2, "consumers
    must ignore unknown fields"): a field this build does not know is a
    newer client speaking, and refusing it would make every additive change
    a wire break. The version test above is the strict half of the pair."""
    request = {
        "v": PROTOCOL_VERSION,
        "baseline_id": "the-log",
        "request_id": "r1",
        "verb": "ON_HOLD",
        "payload": {"job": "j"},
        "expect": {"job:j": 1},
    }
    known = parse_envelope(request, addressed="job:j", baseline_id="the-log")
    surplus = parse_envelope(
        {**request, "invented_by_a_later_client": {"deep": [1, 2]}},
        addressed="job:j",
        baseline_id="the-log",
    )
    assert surplus == known  # ignored, not carried and not refused
