"""The one-line `decision` record, the `dispatch` record after it, and control protocol v3
(DL-118).

Normative spec: `docs/runner-design.md` ss7 (journal before effect); `docs/control-protocol.md`
ss2 and ss5 (the version handshake, and what the subscribe stream promises about which records).

The properties, and the contrast that makes each non-vacuous:

  * **Journal before effect.** Killed at every journal append boundary, the log never holds a
    `dispatch` record whose decision is missing, and a successor dispatches each decided start
    exactly once across both logs. The contrast is the boundary either side of the decision
    write: before it the input replays as an application and the successor dispatches it; after
    the dispatch records the successor dispatches nothing again.
  * **Identity at birth.** A `dispatch` record names a uuid4 run_id, and a run's spool evidence
    must name it.
  * **The stream and the wire.** `decision` crosses the backfill/live seam under the guarantee
    `result` had, and a v2 request is refused naming v3.
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
from dsl41.run_root import active_wal
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    PROTOCOL_VERSION,
    EnvelopeError,
    parse_envelope,
)
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_journal import Journal, dispatched_runs, read_journal
from dsl41.runner_startup import _preflight_identities, _reconcile, resume_run, start_run

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


def _records(path: Path) -> list[dict[str, Any]]:
    """Every complete line, without `read_journal`'s header requirement."""
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _dispatches(records: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return [(r["job"], r["run_number"]) for r in records if r.get("rec") == "dispatch"]


def _decided(records: list[dict[str, Any]]) -> set[int]:
    return {r["index"] for r in records if r.get("rec") == "decision"}


# --------------------------------------------- 1. journal before effect (ss7)


class _Killed(Exception):
    """The engine's process, gone at a journal append boundary."""


def _run_dying_at(run_root: Path, append: int | None) -> int:
    """Start one box through a fresh engine, dying just before the `append`-th journal append
    AFTER genesis (None runs it to quiescence). Answers how many records genesis itself wrote.

    `_write` is the WAL's only durability unit, so a process that dies at a boundary leaves
    exactly the records before it and nothing after. The fault is injected in FRONT of the
    write, so the record it would have made does not exist."""
    engine = start_run(
        lower_source(_BOX_JIL),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    journal = engine.journal
    assert journal is not None
    genesis = len(_records(journal.path))
    appends = 0
    original = journal._write

    def counted(record: dict[str, Any]) -> None:
        nonlocal appends
        appends += 1
        if appends == append:
            raise _Killed(f"engine gone before journal append {appends}")
        original(record)

    journal._write = counted  # type: ignore[method-assign]

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
        journal._write = original  # type: ignore[method-assign]
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
    """A successor engine resumes the root, runs its loop, which dispatches the starts the log
    decided and never dispatched, and stops."""

    async def scenario() -> None:
        engine = await _resume(run_root)
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())


def test_pr35_a_crash_at_any_append_dispatches_each_start_once(tmp_path: Path) -> None:
    """Journal before effect: the engine is killed at EVERY journal append boundary, and every
    resulting log, and its successor's, is checked.

    In the crash's log a `dispatch` record never precedes the decision of the input that
    planned it. A successor decides every attempt it holds and dispatches each decided start
    once across both logs: a start the crash took before its `dispatch` record is dispatched by
    the successor, and one after it is not dispatched again."""
    genesis = _run_dying_at(tmp_path / "intact", None)
    intact = read_journal(tmp_path / "intact" / "journal.jsonl")
    kinds = [r["rec"] for r in intact]
    planned = _dispatches(intact)
    assert planned == [("a", 1), ("b", 1)]  # one decision, two starts
    decision_at = kinds.index("decision")
    assert kinds[decision_at + 1 : decision_at + 3] == ["dispatch", "dispatch"]

    for append in range(1, len(kinds) - genesis + 1):
        run_root = tmp_path / f"crash{append}"
        assert _run_dying_at(run_root, append) == genesis
        records = _records(active_wal(run_root))
        assert [r["rec"] for r in records] == kinds[: genesis + append - 1]
        if _dispatches(records):
            assert _decided(records), "a dispatch with no decision before it"

        _take_over(run_root)
        successor = _records(active_wal(run_root))
        assert successor[: len(records)] == records
        attempts = {r["seq"] for r in successor if r.get("rec") == "input"}
        assert attempts <= _decided(successor)
        if append > decision_at - genesis:  # the input was admitted
            assert sorted(_dispatches(successor)) == planned


def test_pr35_a_torn_decision_line_is_cut_before_the_successor_appends(tmp_path: Path) -> None:
    """The boundary INSIDE the line. A real crash can land mid-write, leaving a prefix of the
    decision. That prefix must read as "undecided" and the successor must not append onto it,
    or its first record and the fragment fuse into one corrupt interior line
    (Journal._repair_tail). The successor writes the decision, then dispatches both starts."""
    genesis = _run_dying_at(tmp_path / "intact", None)
    intact = active_wal(tmp_path / "intact").read_bytes().split(b"\n")
    kinds = [json.loads(line)["rec"] for line in intact if line]
    at = kinds.index("decision")
    assert at > genesis
    torn_root = tmp_path / "torn"
    assert _run_dying_at(torn_root, at + 1 - genesis + 1) == genesis  # died after the decision
    path = active_wal(torn_root)
    lines = path.read_bytes().split(b"\n")
    assert json.loads(lines[at])["rec"] == "decision"
    clean = b"\n".join(lines[:at]) + b"\n"
    path.write_bytes(clean + lines[at][: len(lines[at]) // 2])
    assert [r["rec"] for r in read_journal(path)] == kinds[:at]

    lost = json.loads(lines[at])
    _take_over(torn_root)
    after = read_journal(path)  # the successor appended and left a readable log
    assert [r["rec"] for r in after[:at]] == kinds[:at]
    assert path.read_bytes().startswith(clean)
    assert [r["rec"] for r in after[at:]] == ["decision", "dispatch", "dispatch"]
    assert after[at]["index"] == lost["index"]
    assert sorted(_dispatches(after)) == [("a", 1), ("b", 1)]


# --------------------------------------------------- 2. the record kinds (DL-138)


def _wal(run_root: Path) -> Path:
    return run_root / "wal" / "000001.jsonl"


def _one_period(tmp_path: Path) -> Path:
    """A real run root with a decision and its dispatch in it, closed."""
    run_root = tmp_path / "run"
    engine = start_run(
        lower_source(_SOLO_JIL),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        engine.inject(Event(at=T0 + timedelta(minutes=2), kind="KILLJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        await engine.shutdown()

    asyncio.run(scenario())
    assert engine.journal is not None
    engine.journal.close()
    return run_root


def _rewrite(run_root: Path, records: list[dict[str, Any]]) -> Path:
    path = _wal(run_root)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    return path


def test_the_current_record_kinds_are_accepted(tmp_path: Path) -> None:
    """The positive half of D1, and the one that stops the validator from
    being a way to break every reader: a real period's log holds `segment`,
    `input`, `decision`, `dispatch` and their neighbours, and every one of
    them passes."""
    from dsl41.runner_journal import CURRENT_RECS, RETIRED_RECS

    run_root = _one_period(tmp_path)
    records = read_journal(_wal(run_root))
    kinds = {r["rec"] for r in records}
    assert {"segment", "input", "decision", "dispatch"} <= kinds <= CURRENT_RECS
    assert not kinds & set(RETIRED_RECS)


def test_the_retired_record_dialects_refuse_by_name(tmp_path: Path) -> None:
    """D1 (DL-138): a `header` opening, a `result` mid-journal and a
    standalone `effect` each refuse naming the kind AND the entry that
    retired it; the 1.x `leader`, `host` and `effect_result` records name
    2.0.0. A `header` opening in particular must not come back as "missing
    segment record" -- the validator runs first for exactly that reason."""
    run_root = _one_period(tmp_path)
    records = read_journal(_wal(run_root))
    decision = next(r for r in records if r["rec"] == "decision")
    header = {
        "rec": "header",
        "baseline_id": records[0]["baseline_id"],
        "catalog_hash": "0" * 64,
        "state_machine_version": 1,
        "clock_domain": "virtual",
        "started_at": records[0]["at"],
    }
    result = {
        "rec": "result",
        **{k: decision[k] for k in ("index", "request_id", "decision", "reason", "revisions")},
    }
    effect = {"rec": "effect", "effect_id": "e1:SPAWN:j.1", "index": decision["index"]}
    for kind, forged in (
        ("header", [header, *records[1:]]),
        ("result", [*records, result]),
        ("effect", [*records, effect]),
    ):
        path = _rewrite(run_root, forged)
        with pytest.raises(EngineError, match="RETIRED") as caught:
            read_journal(path)
        assert f"`{kind}`" in str(caught.value)
        assert "DL-138" in str(caught.value)
    for kind in ("leader", "host", "effect_result"):
        path = _rewrite(run_root, [*records, {"rec": kind, "at": T0.isoformat()}])
        with pytest.raises(EngineError, match="RETIRED") as caught:
            read_journal(path)
        assert f"`{kind}`" in str(caught.value)
        assert "2.0.0" in str(caught.value)


def test_an_unknown_record_kind_refuses_as_its_own_error(tmp_path: Path) -> None:
    """The third arm, and a DELIBERATE behavior change: `read_journal` used
    to IGNORE a `rec` it did not recognise. Version gating sits on the
    opening `segment`, so an unrecognised kind inside a version-matched
    segment is corruption -- and it is NOT a tombstone, because "this was
    never legal" is a different fact from "this used to be"
    (docs/protocol-evolution.md ss6)."""
    run_root = _one_period(tmp_path)
    records = read_journal(_wal(run_root))
    path = _rewrite(run_root, [*records, {"rec": "wisdom", "seq": 99}])
    with pytest.raises(EngineError, match="unknown record kind 'wisdom'") as caught:
        read_journal(path)
    assert "DL-138" not in str(caught.value)
    assert "RETIRED" not in str(caught.value)


def test_legacy_batch_is_pinned_three_ways_at_the_central_validator(tmp_path: Path) -> None:
    """D1/D2 (DL-138). The field STAYS on the record and the reads are the
    validator's: exactly `False` proceeds, `True` is the retired fold
    dialect and refuses by name, and missing or non-boolean is MALFORMED --
    a distinct error, because a record that cannot say what it is is not an
    old record.

    All three at the CENTRAL validator, so every consumer inherits them."""
    run_root = _one_period(tmp_path)
    records = read_journal(_wal(run_root))
    assert all(r["legacy_batch"] is False for r in records if r["rec"] == "decision")

    def _with(marker: Any, *, drop: bool = False) -> list[dict[str, Any]]:
        out = []
        for record in records:
            if record.get("rec") != "decision":
                out.append(record)
            elif drop:
                out.append({k: v for k, v in record.items() if k != "legacy_batch"})
            else:
                out.append({**record, "legacy_batch": marker})
        return out

    read_journal(_rewrite(run_root, _with(False)))  # proceeds
    with pytest.raises(EngineError, match="RETIRED") as retired:
        read_journal(_rewrite(run_root, _with(True)))
    assert "DL-138" in str(retired.value)
    for malformed in (_with("true"), _with(1), _with(None, drop=True)):
        with pytest.raises(EngineError, match="malformed rather than old") as bad:
            read_journal(_rewrite(run_root, malformed))
        assert "DL-138" not in str(bad.value)


# ------------------------------------------- 3a. identity at birth and readback


# ------------------------------------------- 3. identity at birth and readback


def test_a_dispatch_record_refuses_a_run_id_outside_the_grammar(tmp_path: Path) -> None:
    """A run_id is the ss11a uuid4 string, and the writer refuses any other: an empty or
    freehand string would survive a presence check and then lose to a fallback mint downstream
    (DL-118)."""
    journal = Journal.create(
        tmp_path / "journal.jsonl",
        catalog=lower_source(_SOLO_JIL),
        clock_domain="virtual",
        started_at=T0,
    )
    for bad in ("", "not-a-uuid", "00000000-0000-1000-8000-000000000001"):
        with pytest.raises(EngineError, match="grammar"):
            journal.dispatch("j", 1, run_id=bad, run_dir=None, started_at=T0)
    journal.dispatch("j", 1, run_id=_RID, run_dir=None, started_at=T0)
    journal.close()
    assert [r["rec"] for r in read_journal(tmp_path / "journal.jsonl")] == ["segment", "dispatch"]


def test_the_dispatch_reader_refuses_a_bad_id_and_a_run_bound_twice() -> None:
    """The read-side half: this code never writes a dispatch whose run_id is outside the
    grammar, or one run under two ids, so meeting either is corruption or a foreign writer and
    resume refuses rather than pick one (DL-118)."""
    other = "00000000-0000-4000-8000-000000000002"

    def dispatch(run_id: object) -> dict[str, Any]:
        return {"rec": "dispatch", "job": "j", "run_number": 1, "run_id": run_id}

    assert dispatched_runs([dispatch(_RID), dispatch(_RID)]) == {("j", 1): _RID}
    with pytest.raises(EngineError, match="ss11a"):
        dispatched_runs([dispatch("not-a-uuid")])
    with pytest.raises(EngineError, match="ss11a"):
        dispatched_runs([dispatch(None)])
    with pytest.raises(EngineError, match="disagrees with itself"):
        dispatched_runs([dispatch(_RID), dispatch(other)])


def test_pr36a_the_reconcile_ladder_refuses_a_split_and_appends_nothing(tmp_path: Path) -> None:
    """The whole ladder, run against a real journal-backed engine whose own `dispatch` record
    bound the run's id. A spool record naming a stranger refuses the resume THROUGH
    `_reconcile`, and the refusal appends NOTHING to the WAL."""
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
        assert engine.journal is not None
        before = read_journal(engine.journal.path)
        [(_job, run_id)] = [(k, v) for k, v in dispatched_runs(before).items() if k == ("j", 1)]
        run_dir = run_root / "runs" / "j.1"
        run_dir.mkdir(parents=True)
        (run_dir / "spawn.json").write_text(json.dumps({"run_id": "rid-else"}))
        try:
            with pytest.raises(EngineError, match=f"'rid-else'.*{run_id!r}"):
                await _reconcile(engine, before, T0, settle_seconds=0.0, grace_seconds=0.0)
            assert read_journal(engine.journal.path) == before
        finally:
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())


# ------------------------------------------------- 4. the stream and the wire


def test_subscribe_forwards_decision_at_least_once_across_the_seam(short_root: Path) -> None:
    """PR-49's `decision` half, and control-protocol ss5.

    `decision` carries its attempt's number under `index`, never `seq`
    (DL-89), so the seam's exactly-once dedup -- which keys on `seq` and on
    nothing else -- cannot apply to it and never suppresses one. That is the
    at-least-once guarantee `result` had, inherited by name change alone.
    The stream is checked on both sides of the seam: a decision arrives in
    the backfill, and a decision for an input admitted afterwards arrives
    live, while the seq'd input records stay exactly-once.
    """
    text = "insert_job: sub_job\njob_type: c\ncommand: x\nmachine: m1\n"
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root, text)
        try:
            assert (await _sendevent(server.path, "ON_HOLD", job="sub_job"))["ok"] is True

            async def decided() -> bool:
                return any(
                    r.get("rec") == "decision" for r in read_journal(run_root / "journal.jsonl")
                )

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
                # the seq'd input crossed the seam exactly once
                assert sum(1 for r in backfilled if r.get("kind") == "OFF_HOLD") == 0
                assert sum(1 for r in live if r.get("kind") == "OFF_HOLD") == 1
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


def test_pr36a_the_preflight_refuses_every_identity_split_before_anything_moves(
    tmp_path: Path,
) -> None:
    """One key runs through the WAL and the spool. The preflight sweeps every candidate's
    PRESENT evidence -- spawn.json, status.json -- against the id the `dispatch` record bound,
    BEFORE resume mutates anything. Present evidence that names no id at all is refused too --
    a wrapper always records one. A run the log never dispatched and would not dispatch checks
    nothing; one it would dispatch now has no evidence of its own, so any is a stranger's."""
    engine = Engine(
        lower_source(_SOLO_JIL),
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    run_dir = tmp_path / "j.1"
    run_dir.mkdir()
    candidates = {("j", 1): run_dir}
    bound = {("j", 1): "rid-wal"}

    # a status-only stranger's record: the fate file, no spawn.json at all
    (run_dir / "status.json").write_text(json.dumps({"run_id": "rid-else"}))
    with pytest.raises(EngineError, match="'rid-else'.*'rid-wal'"):
        _preflight_identities(engine, candidates, bound, set())
    (run_dir / "status.json").unlink()

    # present evidence that names NO id: refused, not treated as unknown
    (run_dir / "spawn.json").write_text(json.dumps({"pid": 1}))
    with pytest.raises(EngineError, match="None.*'rid-wal'"):
        _preflight_identities(engine, candidates, bound, set())

    # agreement everywhere passes
    (run_dir / "spawn.json").write_text(json.dumps({"run_id": "rid-wal"}))
    _preflight_identities(engine, candidates, bound, set())

    # never dispatched and not derived: nothing to hold the evidence to
    (run_dir / "spawn.json").write_text(json.dumps({"run_id": "rid-else"}))
    _preflight_identities(engine, candidates, {}, set())
    # derived and not yet dispatched: any evidence is a stranger's
    with pytest.raises(EngineError, match="never dispatched"):
        _preflight_identities(engine, candidates, {}, {("j", 1)})
