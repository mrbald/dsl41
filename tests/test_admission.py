"""Admission: the frozen ss4 order, its decision index, and two-pass replay.

Normative spec: docs/concurrency-model.md ss4 (admission and application),
ss2 (identity), ss6 (the envelope). Stage S2, obligations CM-04, CM-05 and
CM-07. `ssN` in this file always names concurrency-model; the runner-design
sections it touches are spelled out.

What is tested here is the ORDER, because the order is the whole content
and every step of it is one an obvious implementation gets wrong:

  * **the order is the order** (CM-04, CM-05). Timers fire before the gate,
    so a term_run_time kill lands before the status the gate reads. Dedup
    runs before admission, so a retry costs no index and moves no clock.
  * **the frontiers** are two facts, not one. What is admitted is the
    commit point; what is decided is a suffix of it; the gap is the crash
    window.
  * **the decision index** answers an exact retry, refuses a reused id, and
    refuses an id it admitted but never decided.
  * **two-pass replay** (CM-07). A durable decision is authoritative -- a
    rejection is not applied and an application is not re-decided -- and an
    attempt with no result is applied THROUGH the gate, because a decision
    is exactly what it is missing.

Each claim is paired with the contrast that makes it non-vacuous: the same
inputs under a different id, the same journal without its result record.
"""

from __future__ import annotations

import asyncio

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pydantic import ValidationError

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_startup import start_run
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    AdmissionRefused,
    ApplyResult,
    Attempt,
    DecisionIndex,
    Envelope,
    Frontiers,
    RequestCollision,
    fingerprint,
)
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_journal import Journal, read_journal, replay_inputs

T0 = datetime(2026, 7, 1, 8, 0)

#: The deadline is due at exactly the instant the completion below is
#: stamped, which is the interleaving the ordering rule is about: the loop
#: takes an event at or before the next due timer, so a timer due EARLIER
#: fires as its own input and one due at the SAME instant fires inside the
#: event's batch -- where the gate can either see it or not, depending on
#: nothing but the order of two lines.
_TERM_JIL = """\
insert_job: x
job_type: c
command: sleep 300
term_run_time: 2
"""

_SOLO_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"


def _ev(kind: str, minutes: float, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)  # type: ignore[arg-type]


def _attempt(
    index: int, at_min: float, ev: Event | None, *, source: str | None = "control"
) -> Attempt:
    return Attempt(
        index=index,
        at=T0 + timedelta(minutes=at_min),
        request_id=f"r{index}",
        fingerprint=fingerprint(
            baseline_id="b",
            kind=ev.kind if ev is not None else None,
            payload=dict(ev.payload) if ev is not None else {},
            source=source if ev is not None else None,
        ),
        kind=ev.kind if ev is not None else None,
        payload=dict(ev.payload) if ev is not None else {},
        source=source if ev is not None else None,
    )


# ----------------------------------------------------------- the order is the order


def test_cm04_the_deadline_fires_before_the_gate_reads_the_status_it_gates_on() -> None:
    """The kill wins, and it wins because of WHERE the time half of the
    batch is applied (ss4 step 5).

    x is running under a two-minute term_run_time and a completion for its
    current run arrives at exactly that deadline -- a command that trapped
    SIGTERM and exited 0 of its own accord. Applying the batch fires the
    deadline first, so the gate meets an already-terminal job and rejects
    the exit. Gate first, and the exit would overwrite a kill the estate has
    already acted on.

    The rejection is a RECORD, not an absence: its reason names the gate,
    and the revisions on it show what the time half moved while the attempt
    itself moved nothing."""

    async def scenario() -> Engine:
        engine = Engine(
            lower_source(_TERM_JIL),
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},  # inert park: no natural exit
        )
        engine.inject(_ev("STARTJOB", 0, job="x"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["x"].status == "RUNNING"
        engine._enqueue(  # forge the late natural exit as a COMPLETION
            _ev("STATUS", 2, job="x", run_number=1, exit_code=0), source="adapter"
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["x"].status == "TERMINATED"
    assert engine.drops and "already terminal" in engine.drops[0][1]
    rejected = engine.decisions.for_index(engine.frontiers.applied_index)
    assert rejected is not None and rejected.decision == "rejected"
    assert rejected.reason == "job already terminal"
    # the time half of that same batch is what killed x, so the input the
    # gate rejected still moved a revision. Both halves ride ONE record --
    # the attempt's own `at` is the observation -- so a crash cannot land
    # between them (DL-111)
    assert rejected.revisions == {"job:x": engine.oracle.store.revision("job:x")}


def test_a_completion_the_gate_passes_is_applied_and_records_what_it_moved() -> None:
    """The other side of CM-04, so the rejection above is not just what this
    engine does to every completion: with no deadline in the way the same
    shape of input is applied, and its result carries the revisions it
    moved."""

    async def scenario() -> Engine:
        engine = Engine(
            lower_source(_SOLO_JIL),
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j", 1): (60.0, 0)})},
        )
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["j"].status == "SUCCESS"
    assert not engine.drops
    final = engine.decisions.for_index(engine.frontiers.applied_index)
    assert final is not None and final.decision == "applied" and final.reason is None
    assert final.revisions == {"job:j": engine.oracle.store.revision("job:j")}


def test_cm05_an_exact_retry_takes_no_index_and_moves_no_time() -> None:
    """Dedup precedes admission (ss4 step 2).

    The retry here is stamped five minutes after the original and would, if
    admitted, start a second run of a job that has already finished one. It
    is answered from the index instead: no index consumed, no leader
    timestamp assigned, no state moved. The contrast is the same input under
    a DIFFERENT id, which does start that second run -- so the first half is
    not passing because STARTJOB happens to be idempotent."""

    async def scenario() -> Engine:
        engine = Engine(
            lower_source(_SOLO_JIL),
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j", 1): (60.0, 0), ("j", 2): (60.0, 0)})},
        )
        engine.inject(_ev("STARTJOB", 0, job="j"), request_id="start-j")
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        assert engine.oracle.store.job["j"].status == "SUCCESS"
        settled = engine.frontiers

        engine.inject(_ev("STARTJOB", 5, job="j"), request_id="start-j")  # the retry
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        assert engine.frontiers == settled  # no index, no stamp, nothing decided
        assert engine.oracle.store.job["j"].run_number == 1
        assert [request for request, _ in engine.deduped] == ["start-j"]
        assert engine.deduped[0][1].decision == "applied"  # answered from the first

        engine.inject(_ev("STARTJOB", 6, job="j"), request_id="start-j-again")
        await engine.run_until_quiescent(T0 + timedelta(minutes=6))
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["j"].run_number == 2  # the contrast really runs


def test_one_request_id_for_two_different_commands_is_refused_not_applied() -> None:
    """A reused id cannot be answered from the first decision -- the second
    command would apply neither -- so it is a collision, not a retry. The
    fingerprint is what tells them apart (ss6).

    It is refused, and the engine keeps serving (S3): a collision is a client
    error, and letting one confused caller raise through the single-writer
    loop would take the estate down with it. The second command's effect must
    still be absent -- a refusal that quietly applied would be worse than a
    crash."""

    async def scenario() -> Engine:
        engine = Engine(
            lower_source(_SOLO_JIL),
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev("ON_HOLD", 0, job="j"), request_id="same")
        await engine.run_until_quiescent(T0)
        engine.inject(_ev("OFF_HOLD", 1, job="j"), request_id="same")
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        # the loop survived it and still applies well-formed inputs
        engine.inject(_ev("ON_ICE", 2, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert [reason for _, reason in engine.refusals] == [
        "request_id 'same' was admitted for a different command (fingerprint mismatch):"
        " reuse an id only for an exact retry"
    ]
    assert engine.oracle.store.job["j"].on_hold is True  # the OFF_HOLD never applied
    assert engine.oracle.store.job["j"].on_ice is True
    assert engine.frontiers.committed_index == 2  # ON_HOLD and ON_ICE; the collision took no index


def test_the_fingerprint_separates_the_logs_and_the_commands() -> None:
    """ss6: the fingerprint is the complete semantic envelope, so the same
    command against a different LOG is a different command, and so is a
    different payload against the same one.

    The stamp is not tested here and cannot be: `at` is not a parameter, so
    "the fingerprint excludes it" is a fact about this signature rather than
    a behaviour. What the exclusion BUYS is tested where it bites -- the
    retry in the CM-05 test above is stamped five minutes after its
    original, and a fingerprint that counted the stamp would call it a
    reused id rather than a retry."""
    base = fingerprint(baseline_id="b", kind="STARTJOB", payload={"job": "j"}, source="control")
    other_log = fingerprint(
        baseline_id="other", kind="STARTJOB", payload={"job": "j"}, source="control"
    )
    assert other_log != base
    other_job = fingerprint(
        baseline_id="b", kind="STARTJOB", payload={"job": "k"}, source="control"
    )
    assert other_job != base
    other_verb = fingerprint(
        baseline_id="b", kind="KILLJOB", payload={"job": "j"}, source="control"
    )
    assert other_verb != base


def test_a_rejection_carries_its_reason_and_an_application_carries_none() -> None:
    """A rejection with no reason is unactionable -- the operator is told
    their command did not happen and nothing else -- and an application that
    carries one is a rejection recorded under the wrong decision. Neither is
    constructible."""
    with pytest.raises(ValidationError, match="carries its reason"):
        ApplyResult(index=1, request_id="r", decision="rejected")
    with pytest.raises(ValidationError, match="carries its reason"):
        ApplyResult(index=1, request_id="r", decision="applied", reason="gated")


# ------------------------------------------------------------------- the frontiers


def test_the_two_frontiers_are_two_facts() -> None:
    """ss2: admitted and applied move separately, and admitting is the
    commit point -- so applied trails and never leads."""
    frontiers = Frontiers()
    admitted = frontiers.admit(T0)
    assert (admitted.committed_index, admitted.applied_index, admitted.at) == (1, 0, T0)
    applied = admitted.record(1)
    assert (applied.committed_index, applied.applied_index) == (1, 1)


def test_a_decision_without_an_admission_is_refused() -> None:
    """The invariant, stated where it cannot be bypassed: a result for an
    index that was never admitted is not a late decision, it is a lost
    admission -- and every replay that met it would skip an input."""
    with pytest.raises(ValueError, match="a decision without an admission"):
        Frontiers(committed_index=1, applied_index=2)


def test_results_out_of_order_are_refused() -> None:
    """Steps 5-7 do not yield, so results land in admission order. A gap
    here means a decision went missing, not that one is running late."""
    frontiers = Frontiers().admit(T0).admit(T0)
    with pytest.raises(EngineError, match="out of order"):
        frontiers.record(2)


def test_admission_time_never_goes_backwards() -> None:
    """ss4 step 3: the leader timestamp is monotone across inputs already
    admitted but not yet applied. Refused at admission, so nothing is
    appended for an input that could never be applied."""
    frontiers = Frontiers().admit(T0 + timedelta(minutes=5))
    with pytest.raises(EngineError, match="backwards"):
        frontiers.admit(T0)


# --------------------------------------------------------------- the decision index


def test_the_index_answers_an_exact_retry_and_nothing_else() -> None:
    index = DecisionIndex()
    attempt = _attempt(1, 0, _ev("STARTJOB", 0, job="j"))
    index.note(attempt)
    index.record(ApplyResult(index=1, request_id="r1", decision="applied"))
    assert index.lookup("r1", attempt.fingerprint) is not None
    assert index.lookup("unseen", attempt.fingerprint) is None
    with pytest.raises(RequestCollision):
        index.lookup("r1", "a different fingerprint")


def test_an_admitted_but_undecided_id_is_refused_rather_than_re_admitted() -> None:
    """Unreachable while one writer owns the oracle and steps 5-7 do not
    yield -- which is exactly why meeting it means something else is
    writing, and why answering "unseen" would be the wrong guess: the input
    is already in the log and re-admitting it would apply it twice."""
    index = DecisionIndex()
    attempt = _attempt(1, 0, _ev("STARTJOB", 0, job="j"))
    index.note(attempt)
    with pytest.raises(EngineError, match="admitted but undecided"):
        index.lookup("r1", attempt.fingerprint)


# ------------------------------------------------------------ two-pass replay (CM-07)


def _write_log(
    path: Path, jil: str, attempts: list[Attempt], results: list[ApplyResult] = []
) -> list[dict]:
    """A hand-built log: these attempts, and durable results for whichever
    of them a case wants decided."""
    path.parent.mkdir(parents=True, exist_ok=True)
    journal = Journal.create(path, catalog=lower_source(jil), clock_domain="virtual", started_at=T0)
    by_index = {result.index: result for result in results}
    for attempt in attempts:
        journal.admit(attempt)
        if attempt.index in by_index:
            journal.result(by_index[attempt.index])
    journal.close()
    return read_journal(path)


def _as_the_engine_decided(tmp_path: Path, jil: str, attempts: list[Attempt]) -> list[ApplyResult]:
    """The results a live engine WOULD have written for these attempts.

    Cases that need a durable decision take it from here rather than
    hand-writing revision numbers: the numbers are not what those tests are
    about, and a test that guessed them would fail the ss7 divergence check
    for a reason that has nothing to do with the decision it is pinning.
    The one case that IS about the numbers writes a wrong one deliberately."""
    records = _write_log(tmp_path / "dry" / "journal.jsonl", jil, attempts)
    return replay_inputs(Oracle(lower_source(jil)), records).recovered


def test_cm07_a_durably_rejected_attempt_is_not_applied_on_replay(tmp_path: Path) -> None:
    """The reason replay is two-pass. The attempt is in the log because
    admission is the commit point; its rejection is a LATER record, so a
    reader that met the attempt alone could not tell. Pass one finds the
    rejection, pass two honours it.

    The contrast below is the same journal with the result record removed --
    which IS applied, and would be indistinguishable if the decision were
    being re-derived rather than read."""
    attempts = [
        _attempt(1, 0, _ev("STARTJOB", 0, job="j")),
        _attempt(2, 1, _ev("STATUS", 1, job="j", status="SUCCESS")),
    ]
    start_applied = _as_the_engine_decided(tmp_path, _SOLO_JIL, attempts[:1])
    records = _write_log(
        tmp_path / "run" / "journal.jsonl",
        _SOLO_JIL,
        attempts,
        [
            *start_applied,
            ApplyResult(
                index=2, request_id="r2", decision="rejected", reason="refused by an operator"
            ),
        ],
    )

    oracle = Oracle(lower_source(_SOLO_JIL))
    replay = replay_inputs(oracle, records)
    assert oracle.store.job["j"].status == "RUNNING"  # the rejected STATUS never landed
    assert replay.frontiers.applied_index == 2
    assert not replay.recovered  # both decisions were durable

    without_the_rejection = [r for r in records if not (r["rec"] == "result" and r["index"] == 2)]
    fresh = Oracle(lower_source(_SOLO_JIL))
    replay_inputs(fresh, without_the_rejection)
    assert fresh.store.job["j"].status == "SUCCESS"


def test_cm07_an_attempt_admitted_without_a_result_is_applied_through_the_gate(
    tmp_path: Path,
) -> None:
    """The crash window, both ways.

    Two completions are admitted with no result -- the engine died between
    ss4 steps 4 and 7. Admission is the commit point, so both are applied;
    but a decision is exactly what they are missing, so the gate runs on the
    way, and the one whose run has moved on is rejected rather than fed. A
    replay that applied them blindly would overwrite a finished run with the
    report of a dead one."""
    records = _write_log(
        tmp_path / "run" / "journal.jsonl",
        _SOLO_JIL,
        [
            _attempt(1, 0, _ev("STARTJOB", 0, job="j")),
            _attempt(2, 1, _ev("STATUS", 1, job="j", run_number=1, exit_code=0), source="adapter"),
            _attempt(3, 2, _ev("STATUS", 2, job="j", run_number=0, exit_code=0), source="adapter"),
        ],
    )

    oracle = Oracle(lower_source(_SOLO_JIL))
    replay = replay_inputs(oracle, records)
    assert oracle.store.job["j"].status == "SUCCESS"  # the current run's exit landed
    assert [r.decision for r in replay.recovered] == ["applied", "applied", "rejected"]
    assert replay.recovered[2].reason == "run_number mismatch"
    assert replay.frontiers.applied_index == 3


def test_cm07_a_durable_application_is_not_re_decided(tmp_path: Path) -> None:
    """A durable decision is authoritative. This completion is stale by
    today's gate -- its run_number is 0 where the job is on run 1 -- and the
    log says it was applied, so replay applies it. A build whose gate has
    changed must reproduce the history the log records, not the history it
    would write now; anything else means two engines reading one log reach
    two states."""
    stale = _attempt(
        2, 1, _ev("STATUS", 1, job="j", run_number=0, status="SUCCESS"), source="adapter"
    )
    attempts = [_attempt(1, 0, _ev("STARTJOB", 0, job="j")), stale]
    # what the same log says without the durable decision: today's gate
    # rejects this completion, which is what makes the case a case
    assert _as_the_engine_decided(tmp_path, _SOLO_JIL, attempts)[1].decision == "rejected"

    records = _write_log(
        tmp_path / "run" / "journal.jsonl",
        _SOLO_JIL,
        attempts,
        [ApplyResult(index=2, request_id="r2", decision="applied", revisions={"job:j": 2})],
    )
    oracle = Oracle(lower_source(_SOLO_JIL))
    replay_inputs(oracle, records)
    assert oracle.store.job["j"].status == "SUCCESS"
    assert oracle.store.revision("job:j") == 2  # and the log's revision is the one derived


def test_cm07_the_time_half_of_a_rejected_attempt_still_applies(tmp_path: Path) -> None:
    """The case DL-44's amendment added the advance record for, decided by
    record rather than by absence.

    The rejected completion still observed the clock at T0+2, and the kill
    that observation lets fire is a decision the estate has already acted
    on. Skipping the attempt wholesale -- the obvious reading of "it was
    rejected" -- would leave x RUNNING forever.

    Both ways of being rejected are checked, because they are separate code
    paths and only one of them is obvious: the decision recovered at replay
    (no result in the log), and the decision READ from the log, where
    "honour the rejection" is most tempting to implement as `continue`."""
    attempts = [
        _attempt(1, 0, _ev("STARTJOB", 0, job="x")),
        _attempt(2, 2, _ev("STATUS", 2, job="x", run_number=1, exit_code=0), source="adapter"),
    ]
    recovered_log = _write_log(tmp_path / "recovered" / "journal.jsonl", _TERM_JIL, attempts)
    oracle = Oracle(lower_source(_TERM_JIL))
    replay = replay_inputs(oracle, recovered_log)
    assert oracle.store.job["x"].status == "TERMINATED"  # the deadline fired on the way
    assert replay.recovered[1].decision == "rejected"

    durable_log = _write_log(
        tmp_path / "durable" / "journal.jsonl", _TERM_JIL, attempts, replay.recovered
    )
    from_the_record = Oracle(lower_source(_TERM_JIL))
    again = replay_inputs(from_the_record, durable_log)
    assert from_the_record.store.job["x"].status == "TERMINATED"
    assert not again.recovered  # nothing left to decide, and the kill still happened


def test_a_log_this_build_derives_differently_from_is_refused(tmp_path: Path) -> None:
    """concurrency-model ss7's mixed-build hazard, caught where it is cheap.
    Identical inputs that derive different revisions mean this build is not
    the state machine that wrote the log, and every precondition checked
    from here on would be checked against a number the log never
    produced."""
    records = _write_log(
        tmp_path / "run" / "journal.jsonl",
        _SOLO_JIL,
        [_attempt(1, 0, _ev("STARTJOB", 0, job="j"))],
        [ApplyResult(index=1, request_id="r1", decision="applied", revisions={"job:j": 99})],
    )
    with pytest.raises(EngineError, match="replay diverged at index 1"):
        replay_inputs(Oracle(lower_source(_SOLO_JIL)), records)


def test_a_journal_written_before_s2_replays_unchanged(tmp_path: Path) -> None:
    """No format gate was needed, and this is why: a pre-S2 journal carries
    no results, so every attempt in it is applied -- exactly what the
    single-pass reader did. The synthesized ids name each attempt's position
    in the log, which is the only thing that could ever have identified
    them."""
    path = tmp_path / "journal.jsonl"
    path.write_text(
        '{"rec": "header", "catalog_hash": "x", "clock_domain": "virtual",'
        ' "dsl41_version": "0", "started_at": "2026-07-01T08:00:00"}\n'
        '{"rec": "input", "seq": 1, "at": "2026-07-01T08:00:00", "kind": "STARTJOB",'
        ' "payload": {"job": "j"}, "source": "control"}\n'
        '{"rec": "advance", "seq": 2, "at": "2026-07-01T08:01:00"}\n'
    )
    oracle = Oracle(lower_source(_SOLO_JIL))
    replay = replay_inputs(oracle, read_journal(path))
    assert oracle.store.job["j"].status == "RUNNING"
    assert [r.request_id for r in replay.recovered] == ["log:1", "log:2"]


def test_resume_carries_the_log_position_and_still_answers_a_retry(tmp_path: Path) -> None:
    """ss2, end to end: an engine that forgot where the log had reached
    would re-admit under indices the log already used, and would apply a
    retry of a command it had already decided. The retry here crosses an
    engine incarnation, which is the case a per-process cache cannot
    answer.

    It crosses a TERM too, now that S6a allocates one per incarnation, and
    that is what makes it ss4 step 2's own sentence rather than a paraphrase
    of it: an EXACT old-epoch retry -- the identical envelope, resent to a
    leader that has since been superseded -- recovers its original result.
    Exact is the load-bearing word. A client that re-composes at the new
    epoch is not retrying; it is reusing an id for a different command, and
    the next test is what happens to it."""
    from dsl41.runner_startup import resume_run

    run_root = tmp_path / "run"

    async def scenario() -> Engine:
        engine = start_run(
            lower_source(_SOLO_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j", 1): (60.0, 0), ("j", 2): (60.0, 0)})},
        )
        assert engine.epoch == 1  # genesis holds the first term
        sent = Envelope(
            request_id="start-j",
            expect={"job:j": engine.oracle.store.revision("job:j")},
            epoch=engine.epoch,
        )
        first = engine.submit(_ev("STARTJOB", 0, job="j"), sent)
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        assert (await first).decision == "applied"
        assert engine.journal is not None
        await engine.shutdown()
        engine.journal.close()
        admitted = engine.frontiers.committed_index

        resumed = await resume_run(
            lower_source(_SOLO_JIL),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=2)),
            adapters={"CMD": FakeAdapter({("j", 2): (60.0, 0)})},
        )
        assert resumed.epoch == 2  # a new term over the same log
        assert resumed.frontiers.committed_index == admitted
        assert resumed.frontiers.applied_index == admitted
        again = resumed.submit(_ev("STARTJOB", 3, job="j"), sent)  # the same bytes, resent
        await resumed.run_until_quiescent(T0 + timedelta(minutes=3))
        assert (await again).decision == "applied"  # the FIRST one's decision
        await resumed.shutdown()
        assert resumed.journal is not None
        resumed.journal.close()
        return resumed

    resumed = asyncio.run(scenario())
    assert resumed.oracle.store.job["j"].run_number == 1  # answered, not re-run
    assert [request for request, _ in resumed.deduped] == ["start-j"]
    assert resumed.frontiers.committed_index == resumed.frontiers.applied_index


def test_an_unseen_stale_epoch_is_refused_where_a_retry_of_one_is_not(tmp_path: Path) -> None:
    """ss4 step 2's other half, and the reason the epoch check sits AFTER
    the dedup rather than before it. Both requests here name a superseded
    term; they get opposite answers, and the only thing separating them is
    whether the log already holds a decision under that id."""
    from dsl41.runner_startup import resume_run

    run_root = tmp_path / "run"

    async def scenario() -> tuple[Engine, str]:
        engine = start_run(
            lower_source(_SOLO_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j", 1): (60.0, 0)})},
        )
        stale_epoch = engine.epoch
        expect = {"job:j": engine.oracle.store.revision("job:j")}
        assert engine.journal is not None
        await engine.shutdown()
        engine.journal.close()

        resumed = await resume_run(
            lower_source(_SOLO_JIL),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=1)),
            adapters={"CMD": FakeAdapter({("j", 1): (60.0, 0)})},
        )
        assert resumed.epoch > stale_epoch
        unseen = resumed.submit(
            _ev("STARTJOB", 2, job="j"),
            Envelope(request_id="never-seen", expect=expect, epoch=stale_epoch),
        )
        await resumed.run_until_quiescent(T0 + timedelta(minutes=3))
        with pytest.raises(AdmissionRefused) as refusal:
            await unseen
        await resumed.shutdown()
        assert resumed.journal is not None
        resumed.journal.close()
        return resumed, str(refusal.value)

    resumed, message = asyncio.run(scenario())
    assert "is not this leader's" in message
    assert resumed.oracle.store.job["j"].run_number == 0  # nothing started
    # refused, not rejected: it never reached the log (control-protocol ss3)
    records = read_journal(run_root / "journal.jsonl")
    assert not [r for r in records if r.get("request_id") == "never-seen"]
