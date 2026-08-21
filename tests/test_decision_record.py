"""The atomic `decision` record, and control protocol v3 (DL-118).

Normative spec: `docs/period-model.md` ss2.3 (`decision` -- one atomic
batch) and its obligations PR-35 and PR-49; `docs/concurrency-model.md` ss4
step 7 (the decision, its revisions and its outbox entries commit
atomically) and CM-17; `docs/control-protocol.md` ss2 and ss5 (the version
handshake, and what the subscribe stream promises about which records).

ss4 step 7 has always REQUIRED one commit. The file substrate did not keep
it: `result` and each `effect` were separate `_write` calls, each fsyncing
on its own, so a real window existed in which the decision was durable and
the intent it implied was not -- recovery would find every attempt decided
and an empty outbox, with the terminal row's command still alive. The
existing test proved the two records were written in the right ORDER, which
an engine can do and still die between the fsyncs. This file holds the
stronger claim.

The four properties, and the contrast that makes each non-vacuous:

  * **Together or not at all.** Killed at every journal append boundary in
    the step-7 path, the log never holds a decided input whose intents are
    missing, and never an intent no decision wanted. The contrast is the
    boundary either side of the decision write: before it the input replays
    as an application, after it the intent comes back.
  * **Admission order survives the round trip.** A SPAWN precedes its run's
    later KILL, in the record and in the outbox rebuilt from it.
  * **A pre-DL-118 journal still reads.** Its `result` + `effect` records
    fold into the same state its decision-dialect twin reaches, so an old
    run root keeps replaying, resuming and reporting.
  * **The stream and the wire.** `decision` crosses the backfill/live seam
    under the guarantee `result` had, and a v2 request is refused naming v3.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import uuid

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from test_runner_control import (
    _serve,
    _sendevent,
    _teardown,
    _versioned,
    _wait_for_async,
    short_root,
)

from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    PROTOCOL_VERSION,
    ApplyResult,
    EnvelopeError,
    parse_envelope,
)
from dsl41.runner import Engine
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_effects import Effect
from dsl41.runner_hosts import HostCommand
from dsl41.runner_journal import Journal, read_journal, read_outbox
from dsl41.period import active_wal
from dsl41.runner_startup import (
    _preflight_identities,
    _reconcile,
    _reconcile_applied_spawns,
    resume_run,
    start_run,
)

__all__ = ["short_root"]  # a fixture borrowed from the control tier, re-exported

T0 = datetime(2026, 7, 1, 8, 0)

_SOLO_JIL = "insert_job: j\njob_type: c\ncommand: x\n"

#: a fixed run_id in the ss11a uuid4 grammar -- the writer and reader refuse
#: freehand strings, so fixtures that pass through either must speak it
_RID = "00000000-0000-4000-8000-000000000001"

#: two members under one box, so ONE admitted input plans TWO effects. That
#: is the representative batch: the window the old shape left was between
#: the decision and an effect AND between one effect and the next, and a
#: one-effect batch cannot exhibit the second.
_BOX_JIL = (
    "insert_job: nightly\njob_type: b\n\n"
    "insert_job: a\njob_type: c\ncommand: x\nbox_name: nightly\n\n"
    "insert_job: b\njob_type: c\ncommand: x\nbox_name: nightly\n"
)


def _records(path: Path) -> list[dict[str, Any]]:
    """Every complete line, without `read_journal`'s header requirement: a
    crash at the first append leaves a file with no header, and refusing to
    look at it would hide the one boundary where the log says nothing."""
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _effects_of(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [effect for r in records if r.get("rec") == "decision" for effect in r["effects"]]


def _intents_by_index(records: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """Every intent the log holds, under the index of the decision that
    wanted it -- read in BOTH dialects on purpose. The property is about
    what a crash can separate, not about which record shape is in use, and
    an assertion that could only fail on the shape would pass any writer
    that kept the shape and lost the atomicity."""
    intents: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("rec") == "decision":
            intents.setdefault(record["index"], []).extend(record["effects"])
        elif record.get("rec") == "effect":
            intents.setdefault(record["index"], []).append(
                {k: v for k, v in record.items() if k != "rec"}
            )
    return intents


def _sans_run_id(intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in e.items() if k != "run_id"} for e in intents]


def _decided(records: list[dict[str, Any]]) -> set[int]:
    return {r["index"] for r in records if r.get("rec") == "decision"}


# ------------------------------------------------- 1. PR-35: one commit (CM-17)


class _Killed(Exception):
    """The engine's process, gone at a journal append boundary."""


def _run_dying_at(run_root: Path, append: int | None) -> int:
    """Start one job through a fresh engine, dying just before the `append`-th
    journal append AFTER genesis (None runs it to quiescence). Answers how
    many records genesis itself wrote, so a caller can line the boundary up
    with the log.

    A crash model rather than a metaphor. `_write` is the WAL's only
    durability unit -- one record, one flush, and in the real domain one
    fsync -- so a process that dies at a boundary leaves exactly the records
    before it and nothing after. The fault is injected in FRONT of the
    write, so the record it would have made does not exist, which is the log
    a `kill -9` landing in that window leaves behind. The clock is virtual,
    which is what a process kill (not a power loss) models: the bytes of
    every earlier record are already out of this process. The lock is
    released on the way out because a dead process would have dropped it;
    nothing else about the run root is touched.
    """
    engine = start_run(
        lower_source(_BOX_JIL),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    journal = engine.journal
    assert journal is not None
    genesis = len(_records(journal.path))  # header + leader, before any input
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
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "nightly"}))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))

    try:
        asyncio.run(scenario())
    except _Killed:
        pass
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


def _resumed_outbox_ids(run_root: Path) -> list[str]:
    """The intents a successor engine takes over, from the log alone."""

    async def scenario() -> list[str]:
        engine = await _resume(run_root)
        try:
            return [effect.effect_id for effect in engine.outbox.effects()]
        finally:
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    return asyncio.run(scenario())


def test_pr35_decision_and_effects_commit_together(tmp_path: Path) -> None:
    """PR-35 / CM-17: a decision and its effects survive a crash together or
    not at all.

    The engine is killed at EVERY journal append boundary of the step-7
    path, and every resulting log is checked for the two ways the old shape
    could tear: a decided input whose intents are missing, and an intent no
    decision wanted. The second is now unrepresentable -- there is no record
    an effect can live in on its own -- and the first is what the enumeration
    proves, since the batch is one line and a line is written whole or not
    at all.

    The two named boundaries carry the contrast. Before the decision write
    the log holds the input alone, which replay applies (admission is the
    commit point) and which leaves nothing pending. After it the log holds
    the decision AND both SPAWNs it planned, and the successor takes them
    over. Neither state is the torn one.

    The invariant is read in BOTH dialects so that it is about atomicity
    rather than about the record shape: restore the pre-DL-118 writer (a
    `result` append, then one `effect` append per intent) and this fails at
    `durable == planned` for the boundary between the two, which is the
    window CM-17 names.
    """
    genesis = _run_dying_at(tmp_path / "intact", None)
    intact = read_journal(tmp_path / "intact" / "journal.jsonl")
    kinds = [r["rec"] for r in intact]
    planned = _intents_by_index(intact)
    # non-vacuous, and the batch that makes the enumeration representative:
    # one decision planning more than one effect
    assert any(len(intents) > 1 for intents in planned.values())

    # what each crash left, and what a successor inherited from it. Kept
    # here because taking over a run root APPENDS to it -- a second read of
    # the file would be reading the successor's log, not the crash's.
    left: dict[int, list[dict[str, Any]]] = {}
    inherited: dict[int, list[str]] = {}

    for append in range(1, len(kinds) - genesis + 1):
        run_root = tmp_path / f"crash{append}"
        assert _run_dying_at(run_root, append) == genesis
        records = _records(active_wal(run_root))
        assert [r["rec"] for r in records] == kinds[: genesis + append - 1]

        # the property, both ways round. No decided input has lost an intent
        # it planned, and no intent survives without the decision that
        # wanted it -- the line is the unit, so a partial one is not a state.
        # Compared modulo run_id: the mint is per-transaction (PR-36a), so
        # two engines planning the same batch legitimately mint different
        # ids -- what a crash must not tear is everything else, and each
        # SPAWN's id must exist in ITS log, asserted just below.
        durable = _intents_by_index(records)
        for index in _decided(records):
            assert _sans_run_id(durable.get(index, [])) == _sans_run_id(planned.get(index, []))
            assert all(e["run_id"] for e in durable.get(index, []) if e["kind"] == "SPAWN")
        assert set(durable) <= _decided(records)
        # and this writer cannot even spell the torn state: an intent has no
        # record it could live in on its own
        assert not [r for r in records if r.get("rec") == "effect"]

        left[append] = records
        inherited[append] = _resumed_outbox_ids(run_root)
        assert set(inherited[append]) == {e["effect_id"] for e in _effects_of(records)}

    # appends are 1-based and counted from the first one after genesis, one
    # record each
    decision_append = kinds.index("decision") + 1 - genesis
    assert decision_append > 1  # the input it decides was appended first
    assert decision_append + 1 in left  # and it is not the log's last append

    before = left[decision_append]
    assert [r["rec"] for r in before][-1] == "input"  # admitted, undecided
    assert _effects_of(before) == []
    assert inherited[decision_append] == []

    after = left[decision_append + 1]
    assert [r["rec"] for r in after][-1] == "decision"
    intents = _effects_of(after)
    assert [(e["kind"], e["job"], e["run_number"]) for e in intents] == [
        ("SPAWN", "a", 1),
        ("SPAWN", "b", 1),
    ]
    assert inherited[decision_append + 1] == [e["effect_id"] for e in intents]


def test_pr35_a_torn_decision_line_is_cut_before_the_successor_appends(tmp_path: Path) -> None:
    """The boundary INSIDE the line. The enumeration above kills the engine
    between appends; a real crash can also land mid-write, leaving a prefix
    of the decision. That prefix must read as "undecided" -- which
    `read_journal` already did -- and the successor must not append onto
    it, or its `leader` and the fragment fuse into one corrupt interior line
    and the run root stops reading at all (Journal._repair_tail).

    The torn log and the log cut before the decision write are the same
    state to a successor: input replayed as an application, nothing
    inherited."""
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

    assert _resumed_outbox_ids(torn_root) == []
    after = read_journal(path)  # the successor appended and left a readable log
    assert [r["rec"] for r in after[:at]] == kinds[:at]
    assert path.read_bytes().startswith(clean)
    assert "leader" in [r["rec"] for r in after[at:]]


# --------------------------------------------------- 2. admission order (PR-14)


def test_decision_effects_in_admission_order(tmp_path: Path) -> None:
    """period-model ss2.3 and PR-14: `effects` is in ADMISSION order -- the
    order `Outbox` received them -- so a SPAWN precedes its run's later KILL
    and the KILL never comes first.

    The one-record half is written directly rather than raced out of the
    engine. `plan_effects` decides a KILL from the shell's LIVE runs, and a
    run started by the same input is not live yet, so no single admitted
    input plans both for one run today. The record must carry the order
    anyway -- an adoption fold produces exactly that shape (period-model
    ss11) -- and the reader must keep it, which is what is asserted.

    The engine half is the reachable case: two inputs, two decisions, one
    run, read back from the log in the order they were decided.
    """
    path = tmp_path / "journal.jsonl"
    journal = Journal.create(
        path, catalog=lower_source(_SOLO_JIL), clock_domain="virtual", started_at=T0
    )
    spawn = Effect(
        effect_id="e1:SPAWN:j.1",
        kind="SPAWN",
        job="j",
        run_number=1,
        executor_id="local",
        index=1,
        at=T0,
        run_id=_RID,
        generation=0,
    )
    kill = spawn.model_copy(update={"effect_id": "e1:KILL:j.1", "kind": "KILL"})
    journal.decision(ApplyResult(index=1, request_id="r1", decision="applied"), [spawn, kill])
    journal.close()

    records = read_journal(path)
    [decision] = [r for r in records if r["rec"] == "decision"]
    assert [e["kind"] for e in decision["effects"]] == ["SPAWN", "KILL"]
    rebuilt = read_outbox(records)
    assert [e.kind for e in rebuilt.effects()] == ["SPAWN", "KILL"]
    assert [e.kind for e in rebuilt.pending()] == ["SPAWN", "KILL"]
    # read back, never re-minted -- the KILL copied its run's id
    assert [e.run_id for e in rebuilt.effects()] == [_RID, _RID]

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
    live = read_journal(run_root / "journal.jsonl")
    assert [e["kind"] for e in _effects_of(live)] == ["SPAWN", "KILL"]
    assert [e.kind for e in read_outbox(live).effects()] == ["SPAWN", "KILL"]
    # the two identity binds, from the real planner (period-model ss2.3): the
    # SPAWN minted a uuid4 in the decision transaction (PR-36a), the KILL
    # carries THAT id rather than minting one, and both name the generation
    # of the host row they were born under (PR-16)
    live_spawn, live_kill = _effects_of(live)
    assert uuid.UUID(live_spawn["run_id"]).version == 4
    assert live_kill["run_id"] == live_spawn["run_id"]
    assert live_spawn["generation"] == live_kill["generation"] == 0


# ---------------------------------------- 3. the D1 record validator (DL-138)


def _wal(run_root: Path) -> Path:
    return run_root / "wal" / "000001.jsonl"


def _one_period(tmp_path: Path) -> Path:
    """A real run root with a decision and its effects in it, closed."""
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
    `leader`, `input`, `decision` and their neighbours, and every one of
    them passes.

    `host` is CURRENT and is appended here explicitly. A host command is
    admitted like an input and replays like one, and it is the kind a
    registry pinned from the writers alone would have missed (the engine
    that writes one is exercised in test_hosts.py)."""
    from dsl41.runner_journal import CURRENT_RECS, RETIRED_RECS

    run_root = _one_period(tmp_path)
    records = read_journal(_wal(run_root))
    kinds = {r["rec"] for r in records}
    assert {"segment", "leader", "input", "decision"} <= kinds <= CURRENT_RECS
    assert not kinds & set(RETIRED_RECS)

    host = {
        "rec": "host",
        "seq": 99,
        "at": T0.isoformat(),
        "host": {"verb": "drain", "id": "local", "force": False},
        "source": "control",
    }
    assert read_journal(_rewrite(run_root, [*records, host]))[-1] == host


def test_the_retired_record_dialects_refuse_by_name(tmp_path: Path) -> None:
    """D1 (DL-138): a `header` opening, a `result` mid-journal and a
    standalone `effect` each refuse naming the kind AND the entry that
    retired it. A `header` opening in particular must not come back as
    "missing segment record" -- the validator runs first for exactly that
    reason."""
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
    effect = {"rec": "effect", **decision["effects"][0]}
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


def test_the_retired_fold_reaches_a_history_and_a_retention_consumer(tmp_path: Path) -> None:
    """The inheritance, proved rather than assumed (PR-48's replacement).

    `runner_history` and `retention` parse decision effects WITHOUT
    `read_outbox`, so before D1 each would have needed its own copy of the
    `legacy_batch` rule. Both route through `read_journal`, so both refuse
    the retired fold by name and neither carries a second spelling."""
    from dsl41.retention import plan_retention
    from dsl41.runner_history import RunHistoryError, read_run_root

    run_root = _one_period(tmp_path)
    records = read_journal(_wal(run_root))
    _rewrite(
        run_root,
        [
            {**r, "legacy_batch": True} if r.get("rec") == "decision" else r
            for r in records
        ],
    )
    with pytest.raises(RunHistoryError, match="RETIRED") as history:
        read_run_root(run_root)
    assert "DL-138" in str(history.value)
    with pytest.raises(EngineError, match="RETIRED") as retention:
        plan_retention(run_root)
    assert "DL-138" in str(retention.value)


# ------------------------------------------- 3a. identity at birth and readback


def test_a_native_decision_refuses_an_effect_without_its_identity(tmp_path: Path) -> None:
    """The model's None defaults exist so pre-DL-118 records validate on
    READ; a fresh effect reaching the writer without its identity is a
    planner bug, and writing it would smuggle the legacy shape out under
    `legacy_batch: false` -- so `Journal.decision` refuses (PR-16, PR-36a),
    naming the effect."""
    journal = Journal.create(
        tmp_path / "journal.jsonl",
        catalog=lower_source(_SOLO_JIL),
        clock_domain="virtual",
        started_at=T0,
    )
    base = dict(job="j", run_number=1, executor_id="local", index=1, at=T0)
    idless_spawn = Effect(effect_id="e1:SPAWN:j.1", kind="SPAWN", generation=0, **base)
    generationless = Effect(effect_id="e1:KILL:j.1", kind="KILL", run_id=_RID, **base)
    empty_id = Effect(effect_id="e1:SPAWN:j.1", kind="SPAWN", generation=0, run_id="", **base)
    result = ApplyResult(index=1, request_id="r1", decision="applied")
    with pytest.raises(EngineError, match="e1:SPAWN:j.1.*run_id"):
        journal.decision(result, [idless_spawn])
    with pytest.raises(EngineError, match="e1:KILL:j.1"):
        journal.decision(result, [generationless])
    # an empty or freehand string is not an identity: it would survive a
    # presence check and then lose to an `or`-style fallback downstream
    with pytest.raises(EngineError, match="grammar"):
        journal.decision(result, [empty_id])
    journal.close()
    # neither refused batch left a line behind
    assert [r["rec"] for r in read_journal(tmp_path / "journal.jsonl")] == ["segment"]


def _spool_engine() -> Engine:
    return Engine(
        lower_source(_SOLO_JIL),
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )


def _bound_spawn(run_id: str | None) -> Effect:
    return Effect(
        effect_id="e1:SPAWN:j.1",
        kind="SPAWN",
        job="j",
        run_number=1,
        executor_id="local",
        index=1,
        at=T0,
        run_id=run_id,
        generation=0,
    )


def test_pr36a_the_preflight_refuses_every_identity_split_before_anything_moves(
    tmp_path: Path,
) -> None:
    """One key runs through the WAL and the spool. The preflight sweeps
    every candidate's PRESENT evidence -- spawn.json, status.json, the
    supervisor's LIST row (alive or dead) -- against the id the durable
    effect bound, BEFORE the barrier mutates anything: a refusal has
    appended nothing and resolved nothing. Present evidence that names no
    id at all is refused too -- a new-writer wrapper always records one. A
    pre-DL-118 effect (no bound id) checks nothing."""
    engine = _spool_engine()
    effect = _bound_spawn("rid-wal")
    engine.outbox.record(effect)
    run_dir = tmp_path / "j.1"
    run_dir.mkdir()

    # a status-only stranger's record: the fate file, no spawn.json at all
    (run_dir / "status.json").write_text(json.dumps({"run_id": "rid-else"}))
    with pytest.raises(EngineError, match="'rid-else'.*'rid-wal'"):
        _preflight_identities(engine, {("j", 1): run_dir}, {})
    assert [e.effect_id for e in engine.outbox.pending()] == ["e1:SPAWN:j.1"]  # untouched
    (run_dir / "status.json").unlink()

    # present evidence that names NO id: refused, not treated as unknown
    (run_dir / "spawn.json").write_text(json.dumps({"pid": 1}))
    with pytest.raises(EngineError, match="None.*'rid-wal'"):
        _preflight_identities(engine, {("j", 1): run_dir}, {})

    # a DEAD supervisor row with no local directory is a claim like any other
    with pytest.raises(EngineError, match="supervisor's LIST"):
        _preflight_identities(
            engine, {("j", 1): None}, {("j", 1): {"run_id": "rid-else", "wrapper_alive": False}}
        )

    # agreement everywhere: reconciliation then proceeds and resolves
    (run_dir / "spawn.json").write_text(json.dumps({"run_id": "rid-wal"}))
    _preflight_identities(engine, {("j", 1): run_dir}, {})
    _reconcile_applied_spawns(engine, {("j", 1): run_dir})
    assert engine.outbox.pending() == []

    legacy = _spool_engine()
    legacy.outbox.record(_bound_spawn(None))
    (run_dir / "spawn.json").write_text(json.dumps({"run_id": "rid-else"}))
    _preflight_identities(legacy, {("j", 1): run_dir}, {})
    _reconcile_applied_spawns(legacy, {("j", 1): run_dir})
    assert legacy.outbox.pending() == []


def test_read_outbox_refuses_a_native_decision_with_an_identity_less_effect() -> None:
    """The read-side half of the writer's refusal: this code never writes a
    decision whose SPAWN has no run_id, so meeting one means corruption or a
    foreign writer -- and accepting it would let resume mint an identity
    AFTER the transaction, the exact hole DL-118 closed.

    The fold half of this test went with the `legacy_batch: true` dialect
    at DL-138; `legacy_batch` is now the central validator's, pinned three
    ways there."""
    naked = {
        "effect_id": "e1:SPAWN:j.1",
        "kind": "SPAWN",
        "job": "j",
        "run_number": 1,
        "executor_id": "local",
        "index": 1,
        "at": T0.isoformat(),
    }
    decision = {
        "rec": "decision",
        "index": 1,
        "request_id": "r1",
        "decision": "applied",
        "reason": None,
        "revisions": {},
        "legacy_batch": False,
        "effects": [naked],
    }
    with pytest.raises(EngineError, match="e1:SPAWN:j.1"):
        read_outbox([decision])
    # the record has exactly one shape: a LIST of effects. An absent list
    # must not read as "empty native decision" -- that is intents silently
    # lost.
    with pytest.raises(EngineError, match="effects"):
        read_outbox([{k: v for k, v in decision.items() if k != "effects"}])


def test_read_outbox_refuses_a_decision_carrying_a_malformed_effect() -> None:
    """`generation` is STRICT (Effect docstring): lax pydantic would wave
    `false` through every exact-0 comparison the fold makes below it, so a
    decision carrying one is not a shape this writer ever produced --
    refused as a malformed effect, not silently coerced to generation 0."""
    malformed = {
        "effect_id": "e1:SPAWN:j.1",
        "kind": "SPAWN",
        "job": "j",
        "run_number": 1,
        "executor_id": "local",
        "index": 1,
        "at": T0.isoformat(),
        "run_id": _RID,
        "generation": False,
    }
    decision = {
        "rec": "decision",
        "index": 1,
        "request_id": "r1",
        "decision": "applied",
        "reason": None,
        "revisions": {},
        "legacy_batch": False,
        "effects": [malformed],
    }
    with pytest.raises(EngineError, match="malformed effect"):
        read_outbox([decision])


def test_read_outbox_refuses_a_run_id_outside_the_ss11a_grammar() -> None:
    """Any run_id a decision carries is checked against the ss11a grammar,
    not just against Effect's own field type (a bare `str`): a freehand
    string would pass that type check and then diverge from every real
    identity comparison downstream."""
    effect = {
        "effect_id": "e1:SPAWN:j.1",
        "kind": "SPAWN",
        "job": "j",
        "run_number": 1,
        "executor_id": "local",
        "index": 1,
        "at": T0.isoformat(),
        "run_id": "not-a-uuid",
        "generation": 0,
    }
    decision = {
        "rec": "decision",
        "index": 1,
        "request_id": "r1",
        "decision": "applied",
        "reason": None,
        "revisions": {},
        "legacy_batch": False,
        "effects": [effect],
    }
    with pytest.raises(EngineError, match="ss11a grammar"):
        read_outbox([decision])


def test_pr36a_the_reconcile_barrier_refuses_a_split_and_appends_nothing(tmp_path: Path) -> None:
    """The whole barrier, run against a real journal-backed engine whose own
    decision bound the run's id -- and against the state that actually
    distinguishes the ORDER: a durable SPAWN still PENDING (held by a host
    drain, so no `effect_result` ever landed). A spool record naming a
    stranger refuses the resume THROUGH `_reconcile`, and the refusal
    appends NOTHING to the WAL and resolves nothing. Move the preflight
    below `_reconcile_applied_spawns` and this fails: the pending SPAWN
    with a run directory is exactly what that step durably resolves."""
    run_root = tmp_path / "run"
    engine = start_run(
        lower_source(_SOLO_JIL),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        # drain the one executor FIRST: the SPAWN the decision plans is then
        # held pending by the routing gate -- durable intent, no outcome
        engine.inject_host(HostCommand(verb="drain", host_id="local"))
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        [spawned] = [e for e in engine.outbox.pending() if e.kind == "SPAWN"]
        assert spawned.run_id is not None
        assert engine.journal is not None
        before = read_journal(engine.journal.path)
        assert not [r for r in before if r.get("rec") == "effect_result"]  # pending, provably
        run_dir = run_root / "runs" / "j.1"
        run_dir.mkdir(parents=True)
        (run_dir / "spawn.json").write_text(json.dumps({"run_id": "rid-else"}))
        try:
            with pytest.raises(EngineError, match=f"'rid-else'.*{spawned.run_id!r}"):
                await _reconcile(engine, [], T0, settle_seconds=0.0, grace_seconds=0.0)
            # nothing moved: no append, no resolution -- the run root is
            # exactly as the crash left it
            assert read_journal(engine.journal.path) == before
            assert [e.effect_id for e in engine.outbox.pending()] == [spawned.effect_id]
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
                assert ack == {"ok": True, "subscribed": True}

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
                assert first == {"ok": True, "subscribed": True}, first
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
        "epoch": 0,
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
        "epoch": 0,
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
