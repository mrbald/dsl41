"""Mandatory preconditions and protocol v2 (stage S3).

Normative spec: docs/concurrency-model.md ss0 (the invariant), ss4 (the
admission order), ss6 (the envelope and the reads); docs/control-protocol.md
for the wire. `ssN` in this file always names concurrency-model.

ss0 is one sentence and everything here serves it:

    No externally requested DIRECT mutation of published oracle state is
    applied without checking the version the caller read.

The word that costs something is *mandatory*. A precondition system with an
opt-out is a precondition system nobody uses under pressure, so a caller
that names no revision is refused rather than admitted unchecked -- and the
tests below spend most of their length on the refusals, because the check
itself is one comparison and the mandate is the part an implementation
erodes.

Four distinctions carry the design, and each is tested with the contrast
that makes it non-vacuous:

  * **refused vs rejected.** A refusal (steps 1-2) never entered the log:
    no index, no clock, nothing to replay. A rejection (step 6) is a
    DECISION -- it consumed an index and its time half fired timers -- and
    is recorded as one. Confusing them is how "your command did nothing"
    becomes indistinguishable from "the engine crashed before deciding".
  * **the caller's revision vs a fresh read.** A precondition is only worth
    something if it names what the operator SAW. The tests pin a stale
    revision by hand, because a read-then-write can only ever agree with
    itself.
  * **expect is part of the command.** The same verb at two revisions is
    two commands, so it is in the fingerprint -- otherwise a retry of one
    could be answered by the other's decision, through the dedup path.
  * **dedup before the epoch check** (ss4 step 2). An exact old-epoch retry
    recovers its original result; an unseen old-epoch request is refused.
    The order is counter-intuitive and is pinned here rather than left for
    S6 to rediscover.
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

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import CarriedRows, Event
from dsl41.runner import Engine
from dsl41.runner_startup import start_run
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    PROTOCOL_VERSION,
    AdmissionRefused,
    Attempt,
    Envelope,
    EnvelopeError,
    addressed_key,
    fingerprint,
    parse_envelope,
    stale_reason,
)
from dsl41.runner_clock import RealClock, VirtualClock
from dsl41.runner_hosts import HostCommand
from dsl41.runner_control import APPLIED, REFUSED, REJECTED, UNKNOWN, ControlServer, outcome_of
from dsl41.runner_journal import read_journal, replay_inputs
from subprocess_harness import cli

T0 = datetime(2026, 7, 1, 8, 0)

_SOLO_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"

#: term_run_time 2 puts the deadline at exactly the instant the operator's
#: command below is stamped -- the interleaving ss4 step 5 exists for.
_TERM_JIL = "insert_job: x\njob_type: c\ncommand: sleep 300\nterm_run_time: 2\n"


def _ev(kind: str, minutes: float, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)  # type: ignore[arg-type]


def _envelope(request_id: str, key: str, revision: int, **rest: object) -> Envelope:
    return Envelope(request_id=request_id, expect={key: revision}, **rest)  # type: ignore[arg-type]


def _engine(jil: str = _SOLO_JIL, **kwargs: object) -> Engine:
    return Engine(
        lower_source(jil),
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        **kwargs,  # type: ignore[arg-type]
    )


def _request(**fields: object) -> dict:
    """A well-formed v2 command, for the refusal tests to spoil one field of
    at a time. Every refusal below differs from THIS by exactly one thing."""
    return {
        "v": PROTOCOL_VERSION,
        "cmd": "sendevent",
        "baseline_id": "the-log",
        "epoch": 0,
        "request_id": "r1",
        "verb": "ON_HOLD",
        "payload": {"job": "j"},
        "expect": {"job:j": 3},
    } | fields


# ------------------------------------------------------- 1. the envelope (ss6)


def test_the_addressed_entity_is_the_one_the_verb_names() -> None:
    """ss6's key space, which is the store's own: a precondition is a lookup,
    not a translation."""
    assert addressed_key("ON_HOLD", {"job": "nightly"}) == "job:nightly"
    assert addressed_key("CHANGE_STATUS", {"job": "FEED^PRD", "status": "SUCCESS"}) == (
        "job:FEED^PRD"
    )
    assert addressed_key("SET_GLOBAL", {"name": "FLAG", "value": "go"}) == "global:FLAG"


def test_a_well_formed_envelope_parses_to_exactly_what_it_said() -> None:
    envelope = parse_envelope(
        _request(claimed_actor="alice@host"), addressed="job:j", baseline_id="the-log"
    )
    assert envelope == Envelope(
        request_id="r1", expect={"job:j": 3}, epoch=0, claimed_actor="alice@host"
    )


def test_a_mutation_that_names_no_revision_is_refused() -> None:
    """ss0's whole content. The message names the key and the shape, because
    a refusal an operator cannot act on is one they will route around."""
    request = _request()
    del request["expect"]
    with pytest.raises(EnvelopeError, match=r'expect is required.*\{"job:j": N\}'):
        parse_envelope(request, addressed="job:j", baseline_id="the-log")


def test_there_is_no_any_escape() -> None:
    """ss0 names this one explicitly: a wildcard is an opt-out with a nicer
    spelling, so the shapes that would express one are refused like any
    other malformed precondition."""
    for escape in ("any", "*", None, {}, {"job:j": "any"}, {"job:j": -1}, {"job:j": True}):
        with pytest.raises(EnvelopeError):
            parse_envelope(_request(expect=escape), addressed="job:j", baseline_id="the-log")


def test_expect_names_the_addressed_entity_and_nothing_else() -> None:
    """ss6. A command whose success depended on entities it does not touch
    could not be written usefully -- the semantics move those constantly (a
    box cascade bumps every member), so the only preconditions anyone could
    write would be ones that spuriously fail."""
    for wrong in ({"job:other": 3}, {"job:j": 3, "job:other": 1}, {"global:j": 3}):
        with pytest.raises(EnvelopeError, match="addresses 'job:j'"):
            parse_envelope(_request(expect=wrong), addressed="job:j", baseline_id="the-log")


def test_a_revision_read_from_another_baseline_is_refused() -> None:
    """ss4 step 1. Revision 3 of `job:j` in one log has nothing to do with
    revision 3 in another, so a client aimed at a re-baselined run root must
    be told, not silently believed."""
    with pytest.raises(EnvelopeError, match="not this run's"):
        parse_envelope(_request(baseline_id="some-other-log"), addressed="job:j", baseline_id="b")


def test_a_request_id_is_required() -> None:
    """Without one a timed-out command cannot be retried safely: nothing
    could recognise the retry, so the retry would be a second command."""
    for bad in (None, "", 7):
        request = _request(request_id=bad)
        if bad is None:
            del request["request_id"]
        with pytest.raises(EnvelopeError, match="request_id is required"):
            parse_envelope(request, addressed="job:j", baseline_id="the-log")


def test_epoch_is_required_rather_than_defaulted() -> None:
    """ss6 ships `epoch` in v2 though it is inert on one host, so that
    clients CARRY it. Defaulting an omitted one would mean nobody ever sends
    it, and S6 would then be the second wire break that shipping it early
    was meant to avoid. Every read publishes the current epoch beside the
    revision, so a caller that can compose an `expect` already has it."""
    request = _request()
    del request["epoch"]
    with pytest.raises(EnvelopeError, match="epoch is required"):
        parse_envelope(request, addressed="job:j", baseline_id="the-log")
    with pytest.raises(EnvelopeError, match="epoch is required"):
        parse_envelope(_request(epoch="7"), addressed="job:j", baseline_id="the-log")


def test_an_unversioned_or_wrongly_versioned_request_is_refused() -> None:
    for bad in (None, 1, 2, "3", 4):
        request = _request(v=bad)
        if bad is None:
            del request["v"]
        with pytest.raises(EnvelopeError, match="this engine speaks v3"):
            parse_envelope(request, addressed="job:j", baseline_id="the-log")


def test_the_fingerprint_makes_two_revisions_two_commands() -> None:
    """ss6: `expect` is part of the semantic envelope. "kill the run I saw at
    12" is not "kill whatever is running now", and hashing them alike would
    let a retry of the first be answered by the second's decision."""
    base = {"baseline_id": "b", "kind": "KILLJOB", "payload": {"job": "j"}, "source": "control"}
    at_12 = fingerprint(**base, expect={"job:j": 12})  # type: ignore[arg-type]
    at_13 = fingerprint(**base, expect={"job:j": 13})  # type: ignore[arg-type]
    unconditional = fingerprint(**base)  # type: ignore[arg-type]
    assert len({at_12, at_13, unconditional}) == 3
    assert at_12 == fingerprint(**base, expect={"job:j": 12})  # type: ignore[arg-type]
    # and the actor is in it too: two operators sending the same words are
    # not one command with two deliveries
    assert fingerprint(**base, expect={"job:j": 12}, claimed_actor="alice") != at_12  # type: ignore[arg-type]


# ------------------------------------------------- 2. the check (ss4 step 6)


def _submitted(engine: Engine, ev: Event, envelope: Envelope, horizon_min: float = 10):
    """Submit one command and run the loop past it; return its decision."""

    async def go():
        future = engine.submit(ev, envelope)
        await engine.run_until_quiescent(T0 + timedelta(minutes=horizon_min))
        return await future

    return go


def test_cm06_a_command_composed_against_the_current_revision_applies() -> None:
    async def scenario() -> None:
        engine = _engine()
        current = engine.oracle.store.revision("job:j")
        result = await _submitted(
            engine, _ev("ON_HOLD", 1, job="j"), _envelope("r1", "job:j", current)
        )()
        assert result.decision == "applied" and result.reason is None
        # ss3: the input moved the entity it addressed, and the answer says so
        assert result.revisions == {"job:j": current + 1}
        assert engine.oracle.store.job["j"].on_hold is True
        await engine.shutdown()

    asyncio.run(scenario())


def test_cm06_a_command_composed_against_a_stale_revision_is_rejected() -> None:
    """The point of the whole stage. Someone read revision N, something else
    moved the job, and their command is refused rather than applied to a
    state they never saw."""

    async def scenario() -> Engine:
        engine = _engine()
        stale = engine.oracle.store.revision("job:j")
        # the world moves: an ON_ICE from somewhere else lands first
        engine.inject(_ev("ON_ICE", 1, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.revision("job:j") == stale + 1
        result = await _submitted(
            engine, _ev("ON_HOLD", 2, job="j"), _envelope("r1", "job:j", stale, epoch=engine.epoch)
        )()
        assert result.decision == "rejected"
        assert result.reason == (
            f"precondition failed: job:j is at revision {stale + 1}, not the {stale}"
            " this command was composed against"
        )
        assert engine.oracle.store.job["j"].on_hold is False  # it really did not apply
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    # a rejection is a DECISION: it took an index, unlike a refusal
    assert engine.frontiers.applied_index == 2
    assert engine.refusals == []


def test_a_stale_precondition_still_observed_the_clock() -> None:
    """ss4 applies the time half even when the attempt is rejected, and that
    holds for a precondition rejection exactly as it does for a stale
    completion: the caller's command did not apply, but the timers its
    timestamp made due did fire, and the estate acted on them."""

    async def scenario() -> Engine:
        engine = _engine(_TERM_JIL)
        engine.inject(_ev("STARTJOB", 0, job="x"))
        await engine.run_until_quiescent(T0)
        stale = engine.oracle.store.revision("job:x")
        engine.inject(_ev("ON_ICE", 1, job="x"))  # moves it under the caller
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        result = await _submitted(
            engine,
            _ev("ON_HOLD", 2, job="x"),
            _envelope("r1", "job:x", stale),
            horizon_min=2,
        )()
        assert result.decision == "rejected"
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    # the term_run_time deadline is due at the rejected input's own timestamp
    assert engine.oracle.store.job["x"].status == "TERMINATED"


def test_a_deadline_that_fired_as_its_own_input_invalidates_a_stale_command() -> None:
    """The common case, and the one an operator meets: a term_run_time kill
    became due while they were reading, fired as its own admitted input, and
    bumped the job. Their command names the revision from before it and is
    refused."""

    async def scenario() -> Engine:
        engine = _engine(_TERM_JIL)
        engine.inject(_ev("STARTJOB", 0, job="x"))
        await engine.run_until_quiescent(T0)
        read = engine.oracle.store.revision("job:x")  # current when they read
        result = await _submitted(
            engine,
            _ev("KILLJOB", 3, job="x"),  # a minute PAST the deadline at T0+2
            _envelope("r1", "job:x", read),
            horizon_min=3,
        )()
        assert result.decision == "rejected"
        assert "precondition failed" in (result.reason or "")
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["x"].status == "TERMINATED"  # the kill that DID land


def test_a_timer_inside_this_inputs_own_batch_does_not_invalidate_it() -> None:
    """The boundary of the rule, pinned because it is where the obvious
    reading is wrong -- and it is the SAME scenario as above, moved by one
    minute.

    Stamped exactly at the deadline, the kill fires inside this command's own
    batch rather than as an input before it (ss4 step 5). It changes the
    STATUS the semantics see -- which is exactly what CM-04 is about -- but
    not the revision the gate reads: ss3 gives one input one increment,
    applied at commit, so everything this input causes shares its revision.
    That is not a leak in the check, it is what `expect` means. A revision
    that moved because of the caller's OWN input is not something they could
    have read beforehand, so requiring them to name it would make every
    precondition unsatisfiable by construction.

    What still protects them is the semantics, unchanged: the KILLJOB lands
    on a job the deadline has already terminated, and SEM-01 latching decides
    what that means -- concurrency control was never the thing standing
    between an operator and a job that ended a moment ago."""

    async def scenario() -> Engine:
        engine = _engine(_TERM_JIL)
        engine.inject(_ev("STARTJOB", 0, job="x"))
        await engine.run_until_quiescent(T0)
        read = engine.oracle.store.revision("job:x")
        result = await _submitted(
            engine,
            _ev("KILLJOB", 2, job="x"),  # stamped exactly AT the deadline
            _envelope("r1", "job:x", read),
            horizon_min=2,
        )()
        assert result.decision == "applied"
        # one input, one increment -- the deadline and the kill shared it
        assert result.revisions == {"job:x": read + 1}
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["x"].status == "TERMINATED"


class _SteppedClock:
    """A real-domain clock (`virtual = False`) whose wall time the test sets.
    It lets the loop reach its next decision after a deadline was due, which
    the virtual clock never does."""

    virtual = False

    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    def next_sleeper_due(self) -> datetime | None:
        return None

    def pending_sleepers(self) -> int:
        return 0

    async def wait_until(self, t: datetime, interrupt: asyncio.Event | None = None) -> None:
        if t != datetime.max and t > self.t:
            self.t = t

    async def sleep_until(self, t: datetime) -> None:
        await asyncio.Event().wait()  # a run that never ends on its own


async def _decided_late(engine: Engine, clock: _SteppedClock, kind: str, minutes: float):
    """STARTJOB x at T0 (deadline T0+2), then a command stamped at `minutes`
    against the revision read at T0, decided with the wall clock at T0+3:
    the loop reaches the decision a minute after the deadline was due."""
    engine.inject(_ev("STARTJOB", 0, job="x"))
    await engine.run_until_quiescent(T0)
    read = engine.oracle.store.revision("job:x")
    clock.t = T0 + timedelta(minutes=3)
    future = engine.submit(
        _ev(kind, minutes, job="x"),
        Envelope(request_id="r1", expect={"job:x": read}, epoch=engine.epoch),
    )
    await engine.run_until_quiescent(T0 + timedelta(minutes=3))
    return read, await future


@pytest.mark.parametrize("verb", ["ON_HOLD", "FORCE_STARTJOB"])
def test_a_deadline_due_before_a_late_decision_still_invalidates_a_stale_command(
    verb: str,
) -> None:
    """ss0 in the real domain. The command is stamped at T0+2:30, half a
    minute after the deadline, but the loop decides it at T0+3. The deadline
    still fires first, as its own input stamped at its due instant, so the
    command's revision is stale and it is rejected (DL-232). Before, the
    command's own batch fired the deadline and the gate read the pre-batch
    revision: the ON_HOLD landed and the FORCE_STARTJOB started run 2."""

    async def scenario() -> Engine:
        clock = _SteppedClock(T0)
        engine = Engine(
            lower_source(_TERM_JIL), clock=clock, adapters={"CMD": FakeAdapter(default=None)}
        )
        _, result = await _decided_late(engine, clock, verb, 2.5)
        assert result.decision == "rejected"
        assert "precondition failed" in (result.reason or "")
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    row = engine.oracle.store.job["x"]
    assert (row.status, row.run_number, row.on_hold) == ("TERMINATED", 1, False)
    assert [(t.at, t.transition) for t in engine.oracle.trace()][-1] == (
        T0 + timedelta(minutes=2),
        "RUNNING->TERMINATED",
    )


def test_a_late_decision_still_shares_a_batch_with_a_deadline_due_at_its_instant() -> None:
    """The boundary of the rule above, in the real domain: stamped exactly at
    the deadline, the command's own batch fires it, one input takes one
    increment, and the command applies however late the loop decides it."""

    async def scenario() -> None:
        clock = _SteppedClock(T0)
        engine = Engine(
            lower_source(_TERM_JIL), clock=clock, adapters={"CMD": FakeAdapter(default=None)}
        )
        read, result = await _decided_late(engine, clock, "ON_HOLD", 2)
        assert result.decision == "applied"
        assert result.revisions == {"job:x": read + 1}
        assert engine.oracle.store.job["x"].status == "TERMINATED"
        await engine.shutdown()

    asyncio.run(scenario())


def test_a_late_status_after_a_deadline_records_one_kill_and_replays(tmp_path: Path) -> None:
    """The same interleaving with an ungated `CHANGE_STATUS SUCCESS` used to
    put two terminal transitions of one live run in one batch, and
    `Outbox.record` raised on the second KILL after the decision was
    journaled. The deadline now fires as its own input, and the log replays
    (DL-232)."""
    catalog = lower_source(_TERM_JIL)
    clock = _SteppedClock(T0)
    engine = start_run(
        catalog, tmp_path / "run", clock=clock, adapters={"CMD": FakeAdapter(default=None)}
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="x"))
        await engine.run_until_quiescent(T0)
        clock.t = T0 + timedelta(minutes=3)
        engine.inject(_ev("STATUS", 2.5, job="x", status="SUCCESS"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        await engine.shutdown()

    asyncio.run(scenario())
    assert engine.oracle.store.job["x"].status == "SUCCESS"
    [kill] = [e for e in engine.outbox.effects() if e.kind == "KILL"]
    assert (kill.job, kill.run_number, kill.at) == ("x", 1, T0 + timedelta(minutes=2))
    assert engine.journal is not None
    engine.journal.close()

    records = read_journal(tmp_path / "run" / "journal.jsonl")
    # the deadline is its own verbless input, stamped at its due instant
    assert [(r["rec"], r["at"]) for r in records if r["rec"] in ("input", "advance")][-2:] == [
        ("advance", (T0 + timedelta(minutes=2)).isoformat()),
        ("input", (T0 + timedelta(minutes=2.5)).isoformat()),
    ]
    fresh = Oracle(catalog)
    replay = replay_inputs(fresh, records)
    assert [e.effect_id for e in replay.outbox.effects() if e.kind == "KILL"] == [kill.effect_id]
    assert fresh.store.job["x"].status == "SUCCESS"


#: a negative term_run_time arms a deadline already past due at the start
_PAST_DUE_JIL = "insert_job: x\njob_type: c\ncommand: sleep 300\nterm_run_time: -1\n"


def test_a_timer_due_before_the_last_admitted_instant_is_stamped_at_that_instant(
    tmp_path: Path,
) -> None:
    """The stamp's floor (DL-232). The deadline is due at T0-1, before the
    STARTJOB admitted at T0 that armed it, and a command stamped T0+0:30
    waits behind it. The deadline fires as its own input at T0, the later
    instant, so admission never sees time go backwards."""
    # a second past the first horizon: a real clock is never exactly at it,
    # and this one would otherwise wait on an instant that has already passed
    clock = _SteppedClock(T0 + timedelta(seconds=1))
    engine = start_run(
        lower_source(_PAST_DUE_JIL),
        tmp_path / "run",
        clock=clock,
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="x"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["x"].status == "RUNNING"  # the deadline waits
        read = engine.oracle.store.revision("job:x")
        clock.t = T0 + timedelta(minutes=1)
        future = engine.submit(
            _ev("ON_HOLD", 0.5, job="x"),
            Envelope(request_id="r1", expect={"job:x": read}, epoch=engine.epoch),
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert (await future).decision == "rejected"
        await engine.shutdown()

    asyncio.run(scenario())
    assert engine.journal is not None
    engine.journal.close()
    records = read_journal(tmp_path / "run" / "journal.jsonl")
    assert [(r["rec"], r["at"]) for r in records if r["rec"] in ("input", "advance")] == [
        ("input", T0.isoformat()),
        ("advance", T0.isoformat()),
        ("input", (T0 + timedelta(seconds=30)).isoformat()),
    ]
    assert engine.oracle.store.job["x"].status == "TERMINATED"


def test_a_carried_timer_due_before_the_seal_instant_fires_at_that_instant() -> None:
    """A segment opened from a seal has admitted nothing, but its oracle
    starts at the seal instant T. A carried deadline due before T fires as
    its own input stamped at T, not at its due instant, which the oracle
    would refuse as time going backwards (DL-232). The command behind it is
    refused at admission for its epoch, so it takes no index and the
    frontier is the timer's own stamp."""
    catalog = lower_source(_TERM_JIL)
    seal_at = T0 + timedelta(minutes=3)

    async def scenario() -> Engine:
        closing = Engine(
            catalog, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter(default=None)}
        )
        closing.inject(_ev("STARTJOB", 0, job="x"))
        await closing.run_until_quiescent(T0)
        store = closing.oracle.store
        carried = CarriedRows(
            jobs=dict(store.job),
            timers=tuple(store.timers()),
            timer_seq=store.timer_seq,
            period_id=2,
            now=seal_at,
        )
        await closing.shutdown()

        clock = _SteppedClock(seal_at + timedelta(minutes=1))
        opened = Engine(
            catalog, clock=clock, adapters={"CMD": FakeAdapter(default=None)}, carried=carried
        )
        assert [due for due, _, _ in opened.oracle.store.timers()] == [T0 + timedelta(minutes=2)]
        future = opened.submit(
            _ev("ON_HOLD", 3.5, job="x"),
            Envelope(request_id="r1", expect={"job:x": 1}, epoch=opened.epoch + 1),
        )
        await opened.run_until_quiescent(seal_at + timedelta(minutes=1))
        with pytest.raises(AdmissionRefused):
            await future
        await opened.shutdown()
        return opened

    opened = asyncio.run(scenario())
    assert opened.frontiers.at == seal_at
    assert opened.frontiers.applied_index == opened.frontiers.committed_index == 1
    assert opened.oracle.store.job["x"].status == "TERMINATED"


def test_revision_zero_is_the_conditional_create() -> None:
    """ss6/ss3: an entity with no row reads 0, and the catalog seed is itself
    an input -- so anything that exists is at 1 or more. `expect 0` therefore
    means "still absent", and it is the only way to express a create that
    must not clobber."""

    async def scenario() -> None:
        engine = _engine()
        first = await _submitted(
            engine,
            _ev("SET_GLOBAL", 1, name="FLAG", value="go"),
            _envelope("r1", "global:FLAG", 0),
        )()
        assert first.decision == "applied"
        assert engine.oracle.store.global_value("FLAG") == "go"
        second = await _submitted(
            engine,
            _ev("SET_GLOBAL", 2, name="FLAG", value="clobbered"),
            _envelope("r2", "global:FLAG", 0),
            horizon_min=2,
        )()
        assert second.decision == "rejected"
        assert engine.oracle.store.global_value("FLAG") == "go"
        await engine.shutdown()

    asyncio.run(scenario())


def test_the_engines_own_inputs_carry_no_precondition() -> None:
    """ss0 is about what CROSSES the boundary. A cascade, a timer, a
    scheduler tick and an adapter completion are consequences of applying an
    input, and an operator cannot hold a revision on state only the
    semantics may change -- so requiring one of them would be requiring a
    revision nobody could ever have read."""

    async def scenario() -> Engine:
        engine = _engine(_TERM_JIL)
        engine.inject(_ev("STARTJOB", 0, job="x"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["x"].status == "TERMINATED"  # the timer applied
    assert engine.refusals == []


# ------------------------------------------- 3. refused vs rejected (the ledger)


def test_a_refusal_leaves_nothing_in_the_log_and_a_rejection_leaves_a_decision(
    tmp_path: Path,
) -> None:
    """The distinction the whole S3 vocabulary rests on. A refusal is not a
    quiet rejection -- it is the absence of an event, and the log has to say
    so by saying nothing."""

    async def scenario() -> None:
        engine = start_run(
            lower_source(_SOLO_JIL),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        stale = engine.oracle.store.revision("job:j")
        engine.inject(_ev("ON_ICE", 1, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        rejected = await _submitted(
            engine,
            _ev("ON_HOLD", 2, job="j"),
            _envelope("keeps-its-index", "job:j", stale, epoch=engine.epoch),
        )()
        assert rejected.decision == "rejected"

        # and now a REFUSAL: the same id, a different command
        future = engine.submit(
            _ev("OFF_HOLD", 3, job="j"),
            _envelope(
                "keeps-its-index",
                "job:j",
                engine.oracle.store.revision("job:j"),
                epoch=engine.epoch,
            ),
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        with pytest.raises(AdmissionRefused, match="different command"):
            await future
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    records = read_journal(tmp_path / "run" / "journal.jsonl")
    ids = [r.get("request_id") for r in records if r.get("rec") == "input"]
    assert ids.count("keeps-its-index") == 1  # the rejection; the refusal is absent
    result = next(r for r in records if r.get("rec") == "decision" and r["decision"] == "rejected")
    assert "precondition failed" in result["reason"]
    # the attempt records what it was composed against, so replay can re-run
    # the same gate against the same claim
    attempt = next(r for r in records if r.get("request_id") == "keeps-its-index")
    # 0, not 1: a job whose JIL sets no flags projects identically to the
    # default row, so the catalog seed -- itself an input -- changes nothing
    # for it and leaves it at 0. Absence and "seeded but untouched" are the
    # same revision for a job; only a global distinguishes them (ss6).
    assert attempt["expect"] == {"job:j": 0}


# ------------------------------------------------- 4. retry and epoch (ss4 step 2)


def test_an_exact_retry_of_a_rejected_command_gets_the_same_rejection() -> None:
    """A rejection is a decision, so it is idempotent like any other: the
    retry is answered from the index and takes no second index. Otherwise a
    client that retried on timeout would push its rejected command through
    the gate again -- against a state that has since moved, which is how a
    "safe" retry becomes an unintended application."""

    async def scenario() -> Engine:
        engine = _engine()
        stale = engine.oracle.store.revision("job:j")
        engine.inject(_ev("ON_ICE", 1, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        envelope = _envelope("r1", "job:j", stale)
        first = await _submitted(engine, _ev("ON_HOLD", 2, job="j"), envelope)()
        assert first.decision == "rejected"
        admitted = engine.frontiers.committed_index
        second = await _submitted(engine, _ev("ON_HOLD", 3, job="j"), envelope, horizon_min=3)()
        assert second == first  # the same decision object, not a fresh verdict
        assert engine.frontiers.committed_index == admitted  # and no second index
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert [request_id for request_id, _ in engine.deduped] == ["r1"]


def test_an_unseen_request_at_a_stale_epoch_is_refused() -> None:
    """ss4 step 2's second half. A client still talking to a superseded
    leader composed this against a world that has moved on."""

    async def scenario() -> Engine:
        engine = _engine()
        engine.epoch = 7
        future = engine.submit(_ev("ON_HOLD", 1, job="j"), _envelope("r1", "job:j", 0, epoch=3))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        with pytest.raises(AdmissionRefused, match="epoch 3 is not this leader's 7"):
            await future
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["j"].on_hold is False
    assert engine.frontiers.committed_index == 0  # refused: it took no index


def test_an_exact_retry_at_a_stale_epoch_recovers_its_original_decision() -> None:
    """The ordering ss4 step 2 calls out, and the reason dedup is written
    BEFORE the epoch check rather than after it.

    This command was decided by the leader that held epoch 3. The client
    never heard the answer and retries; by then the epoch has moved. Refusing
    the retry would hide a decision that was already made and applied -- the
    client would re-compose and send it a second time. Answering it from the
    index is the only outcome that does not double-apply."""

    async def scenario() -> Engine:
        engine = _engine()
        engine.epoch = 3
        envelope = _envelope("r1", "job:j", 0, epoch=3)
        first = await _submitted(engine, _ev("ON_HOLD", 1, job="j"), envelope)()
        assert first.decision == "applied"
        engine.epoch = 7  # the world elected someone else, and back again
        second = await _submitted(engine, _ev("ON_HOLD", 2, job="j"), envelope, horizon_min=2)()
        assert second == first
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert engine.refusals == []  # answered, not refused
    assert engine.frontiers.committed_index == 1


# ------------------------------------------------------------- 5. replay (ss4)


def test_a_rejected_precondition_replays_as_a_rejection(tmp_path: Path) -> None:
    """A durable decision is authoritative. Replay must not re-run the gate
    and find the precondition satisfiable in the state IT has rebuilt -- the
    log records what happened, not what a fresh evaluation would prefer."""

    async def scenario() -> None:
        engine = start_run(
            lower_source(_SOLO_JIL),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        stale = engine.oracle.store.revision("job:j")
        engine.inject(_ev("ON_ICE", 1, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        result = await _submitted(
            engine, _ev("ON_HOLD", 2, job="j"), _envelope("r1", "job:j", stale, epoch=engine.epoch)
        )()
        assert result.decision == "rejected"
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    records = read_journal(tmp_path / "run" / "journal.jsonl")
    fresh = Oracle(lower_source(_SOLO_JIL))
    replay = replay_inputs(fresh, records)
    assert fresh.store.job["j"].on_hold is False
    assert replay.recovered == []  # every attempt had its decision on disk


def test_an_admitted_precondition_with_no_result_re_runs_the_gate(tmp_path: Path) -> None:
    """The crash window (ss4): admission is the commit point, so an attempt
    with no result is applied -- and it goes through the gate on the way,
    because its decision is exactly what did not survive. The `expect` it was
    admitted under is on the record, which is what makes that possible."""

    async def scenario() -> None:
        engine = start_run(
            lower_source(_SOLO_JIL),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        stale = engine.oracle.store.revision("job:j")
        engine.inject(_ev("ON_ICE", 1, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _submitted(
            engine, _ev("ON_HOLD", 2, job="j"), _envelope("r1", "job:j", stale, epoch=engine.epoch)
        )()
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    path = tmp_path / "run" / "journal.jsonl"
    records = read_journal(path)
    # cut the last decision record: the crash landed between steps 4 and 7
    torn = [r for r in records if not (r.get("rec") == "decision" and r["index"] == 2)]
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in torn))

    fresh = Oracle(lower_source(_SOLO_JIL))
    replay = replay_inputs(fresh, read_journal(path))
    assert [r.decision for r in replay.recovered] == ["rejected"]
    assert "precondition failed" in (replay.recovered[0].reason or "")
    assert fresh.store.job["j"].on_hold is False


# --------------------------------------------------------- 6. the wire (ss6)

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("unix-domain control sockets are POSIX-only", allow_module_level=True)


async def _call(sock_path: Path, request: dict) -> dict:
    return await _call_line(sock_path, json.dumps(request))


async def _call_line(sock_path: Path, line: str) -> dict:
    """One raw request line, so a test can send something the framing itself
    rejects -- which `_call` cannot, since it composes the line."""
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        writer.write(line.encode("utf-8") + b"\n")
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _serve_engine(engine: Engine) -> tuple[Engine, ControlServer, asyncio.Future[Any]]:
    """Bind a control socket at `<engine.run_root>/control.sock`, start it,
    and drive the engine to `datetime.max` -- the shared second half of
    every helper here and in test_recovery.py/test_hosts.py that opens an
    engine (by `start_run` or `resume_run`) and then serves it."""
    assert engine.run_root is not None
    server = ControlServer(engine, engine.run_root / "control.sock")
    await server.start()
    loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    return engine, server, loop_task


async def _serve(run_root: Path, text: str = _SOLO_JIL):
    engine = start_run(
        lower_source(text),
        run_root,
        clock=RealClock(),
        adapters={"CMD": FakeAdapter(default=None)},
        hold_open=True,
    )
    return await _serve_engine(engine)


async def _teardown(engine: Engine, server: ControlServer, loop_task) -> None:
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
    await server.close()
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def test_every_door_refuses_an_unversioned_caller(short_root: Path) -> None:
    """control-protocol ss7 gap 1, closed. The check sits ahead of the
    subscribe branch precisely because subscribe owns its connection and
    never reaches the response path -- an unversioned door there would be
    the one left open."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        try:
            for request in (
                {"cmd": "status"},
                {"cmd": "sendevent", "verb": "ON_HOLD", "payload": {"job": "j"}},
                {"cmd": "subscribe", "since": 0},
                {"cmd": "status", "v": 1},
                {"cmd": "status", "v": 2},  # gone, not deprecated (DL-118)
            ):
                answer = await _call(server.path, request)
                assert answer["ok"] is False
                assert "this engine speaks v3" in answer["error"]
            # the connection stays usable: a refusal is not a hangup
            good = await _call(server.path, {"cmd": "status", "v": PROTOCOL_VERSION})
            assert good["ok"] is True
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_every_read_publishes_the_header_a_precondition_is_composed_from(
    short_root: Path,
) -> None:
    """ss6: reads publish `baseline_id`, `epoch` and `applied_index`. A
    revision is meaningless without the log it came from, and a client that
    cannot name the log cannot be told it is holding a stale one."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        try:
            for cmd in ({"cmd": "status"}, {"cmd": "timers"}, {"cmd": "global", "name": "X"}):
                answer = await _call(server.path, cmd | {"v": PROTOCOL_VERSION})
                assert answer["baseline_id"] == engine.baseline_id != ""
                # the term this run root's ledger allocated, not a constant:
                # a client composes against the leader it read from (S6a)
                assert answer["epoch"] == engine.epoch == 1
                assert answer["applied_index"] == engine.frontiers.applied_index
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_the_socket_answers_with_the_decision_not_the_receipt(short_root: Path) -> None:
    """ss4 emits `command_committed` at step 4 and `oracle_applied` at step
    7. A request/response transport that answered with the first would tell
    an operator their command was written down, not that it landed -- and a
    precondition nobody can see the outcome of is not a precondition."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        try:
            read = await _call(server.path, {"cmd": "status", "job": "j", "v": PROTOCOL_VERSION})
            revision = read["jobs"]["j"]["state_rev"]
            command = {
                "v": PROTOCOL_VERSION,
                "cmd": "sendevent",
                "baseline_id": read["baseline_id"],
                "epoch": read["epoch"],
                "request_id": "wire-1",
                "verb": "ON_HOLD",
                "payload": {"job": "j"},
                "expect": {"job:j": revision},
                "claimed_actor": "alice@host",
            }
            applied = await _call(server.path, command)
            assert applied["ok"] is True
            assert applied["decision"] == "applied"
            assert applied["revisions"] == {"job:j": revision + 1}
            assert applied["request_id"] == "wire-1"
            # the flag really moved, and the answer already knew it
            after = await _call(server.path, {"cmd": "status", "job": "j", "v": PROTOCOL_VERSION})
            assert after["jobs"]["j"]["on_hold"] is True

            # the same envelope again is a retry, answered from its decision
            assert await _call(server.path, command) == applied

            # and a command composed against the revision we ALREADY used is
            # now stale: rejected, with the reason, and exit-worthy ok:false
            stale = await _call(server.path, command | {"request_id": "wire-2"})
            assert stale["ok"] is False and stale["decision"] == "rejected"
            assert "precondition failed" in stale["error"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_a_command_with_no_decision_is_answered_i_do_not_know(short_root: Path) -> None:
    """The third outcome, and the only honest one for it.

    With the single-writer loop not draining, the command may be sitting in
    the queue or may be durably admitted and about to apply -- the server
    cannot tell, and neither can the caller. Answering `ok: false` alone
    would read as "it did not happen", which is the one thing nobody knows.
    So it says what it does not know and what to do about it, and it says so
    BEFORE the client's own timeout, which would only report that something
    did not answer."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        loop_task.cancel()  # the writer stops; the socket keeps serving
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task
        server.DECISION_TIMEOUT_S = 0.2  # the wait is the point, not its length
        try:
            read = await _call(server.path, {"cmd": "status", "job": "j", "v": PROTOCOL_VERSION})
            answer = await _call(
                server.path,
                {
                    "v": PROTOCOL_VERSION,
                    "cmd": "sendevent",
                    "baseline_id": read["baseline_id"],
                    "epoch": read["epoch"],
                    "request_id": "undecided",
                    "verb": "ON_HOLD",
                    "payload": {"job": "j"},
                    "expect": {"job:j": read["jobs"]["j"]["state_rev"]},
                },
            )
            assert answer["ok"] is False
            assert "no decision within" in answer["error"]
            assert "re-read before retrying" in answer["error"]
            # neither applied nor refused: the answer claims no decision, and
            # must not carry one
            assert "decision" not in answer and "refused" not in answer
            # queries still work -- only the writer stopped
            assert (await _call(server.path, {"cmd": "timers", "v": PROTOCOL_VERSION}))["ok"]
        finally:
            await server.close()
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())


def test_the_socket_refuses_a_mutation_that_names_no_revision(short_root: Path) -> None:
    """The mandate, at the only boundary an external caller can reach."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        try:
            read = await _call(server.path, {"cmd": "status", "job": "j", "v": PROTOCOL_VERSION})
            answer = await _call(
                server.path,
                {
                    "v": PROTOCOL_VERSION,
                    "cmd": "sendevent",
                    "baseline_id": read["baseline_id"],
                    "epoch": read["epoch"],
                    "request_id": "no-expect",
                    "verb": "ON_HOLD",
                    "payload": {"job": "j"},
                },
            )
            assert answer["ok"] is False and "expect is required" in answer["error"]
            # `refused`, not a rejection: a machine client must be able to tell
            # "nothing was written" from "a decision went against you", since
            # only the first is safe to re-send unchanged
            assert answer["refused"] is True and "decision" not in answer
            assert engine.oracle.store.job["j"].on_hold is False
            assert engine.frontiers.committed_index == 0  # nothing was admitted
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_an_unknown_job_is_refused_before_its_envelope_is_judged(short_root: Path) -> None:
    """ss4 step 1 validates framing, and the verb's own payload is framing:
    `expect` cannot be checked before the addressed key is known, so the
    catalog check necessarily comes first. An operator who typo'd a job name
    must hear about the typo, not about a precondition on a job that does
    not exist."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        try:
            answer = await _call(
                server.path,
                {
                    "v": PROTOCOL_VERSION,
                    "cmd": "sendevent",
                    "baseline_id": engine.baseline_id,
                    "epoch": engine.epoch,
                    "request_id": "typo",
                    "verb": "STARTJOB",
                    "payload": {"job": "jj"},
                    "expect": {"job:jj": 0},
                },
            )
            assert answer["ok"] is False and answer["error"] == "unknown job 'jj'"
            assert answer["refused"] is True
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ---------------------------------------------------------------- 7. the CLI


def _sendevent_cli(socket_path: Path, *args: str):
    return cli("sendevent", *args, "--socket", str(socket_path))


def test_cli_sendevent_reads_then_writes_and_a_stale_expect_exits_3(short_root: Path) -> None:
    """The shell surface of ss0. Omitted, `--expect` is read immediately
    before the write -- which narrows the race to one round trip and does
    not close it, so `--expect` is how an operator names the revision they
    actually looked at, and a losing command exits nonzero rather than
    applying. It exits 3, not 2, since DL-92: it lost a race that was
    DECIDED and journaled, which is a different fact from never having been
    admitted, and the two call for different next moves."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            ok = await asyncio.to_thread(_sendevent_cli, server.path, "ON_HOLD", "--job", "j")
            assert ok.returncode == 0, ok.stderr
            assert json.loads(ok.stdout)["decision"] == "applied"
            assert engine.oracle.store.job["j"].on_hold is True

            stale = await asyncio.to_thread(
                _sendevent_cli, server.path, "OFF_HOLD", "--job", "j", "--expect", "0"
            )
            assert stale.returncode == 3
            assert "precondition failed" in json.loads(stale.stdout)["error"]
            assert engine.oracle.store.job["j"].on_hold is True  # unchanged
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_the_journal_records_what_the_caller_claimed_to_be(short_root: Path) -> None:
    """ss6 calls it `claimed_actor` and means it: there is no authentication
    at this tier (control-protocol ss7 gap 2), so the log records what the
    client SAID about itself. It is a breadcrumb, and naming it a claim is
    what keeps it from being read as attribution."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            read = await _call(server.path, {"cmd": "status", "job": "j", "v": PROTOCOL_VERSION})
            answer = await _call(
                server.path,
                {
                    "v": PROTOCOL_VERSION,
                    "cmd": "sendevent",
                    "baseline_id": read["baseline_id"],
                    "epoch": read["epoch"],
                    "request_id": "claim-1",
                    "verb": "ON_HOLD",
                    "payload": {"job": "j"},
                    "expect": {"job:j": read["jobs"]["j"]["state_rev"]},
                    "claimed_actor": "not-really-root@somewhere",
                },
            )
            assert answer["ok"] is True
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())
    record = next(
        r for r in read_journal(run_root / "journal.jsonl") if r.get("request_id") == "claim-1"
    )
    assert record["claimed_actor"] == "not-really-root@somewhere"
    assert record["expect"] == {"job:j": 0}


def test_a_caller_parked_on_a_decision_is_told_when_the_engine_stops() -> None:
    """A command queued but never admitted has no entry in the log and never
    will. Leaving its caller to wait out a transport timeout would tell them
    only that something did not answer -- which is the one thing they must
    not conclude, since a timeout is also what an admitted-but-slow command
    looks like."""

    async def scenario() -> None:
        engine = _engine()
        future = engine.submit(_ev("ON_HOLD", 1, job="j"), _envelope("r1", "job:j", 0))
        await engine.shutdown()  # without ever running the loop
        with pytest.raises(AdmissionRefused, match="shut down before this input was admitted"):
            await future

    asyncio.run(scenario())


# ------------------------------------------- 8. the four outcomes (S4, DL-92)


def _query_cli(socket_path: Path, *args: str):
    import subprocess

    return subprocess.run(
        [sys.executable, "-m", "dsl41", "query", *args, "--socket", str(socket_path)],
        capture_output=True,
        text=True,
    )


def test_every_ok_false_a_mutation_can_meet_says_whether_it_was_admitted(
    short_root: Path,
) -> None:
    """`outcome_of` reads the ABSENCE of `refused` as uncertainty, so that
    absence has to mean exactly one thing on the wire. This sweeps every
    ok:false answer a sendevent can be given -- including the two doors it
    shares with the queries, which is where an unmarked refusal would
    otherwise hide -- and pins that each one says nothing was admitted."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        try:
            read = await _call(server.path, {"cmd": "status", "job": "j", "v": PROTOCOL_VERSION})
            rev = read["jobs"]["j"]["state_rev"]

            def envelope(**over: object) -> dict:
                return {
                    "v": PROTOCOL_VERSION,
                    "cmd": "sendevent",
                    "baseline_id": read["baseline_id"],
                    "epoch": read["epoch"],
                    "request_id": "sweep-1",
                    "verb": "ON_HOLD",
                    "payload": {"job": "j"},
                    "expect": {"job:j": rev},
                } | over

            refusals = {
                "unversioned": envelope(v=None),
                "unknown cmd": envelope(cmd="nonsense"),
                "unknown job": envelope(payload={"job": "ghost"}),
                "no expect": envelope(expect=None),
                "foreign baseline": envelope(baseline_id="someone-elses-run"),
                "stale epoch": envelope(epoch=read["epoch"] - 1, request_id="sweep-2"),
                "bad status": envelope(verb="CHANGE_STATUS", payload={"job": "j", "status": "?"}),
            }
            for label, request in refusals.items():
                answer = await _call(server.path, request)
                assert answer["ok"] is False, label
                assert answer["refused"] is True, label
                assert outcome_of(answer) == REFUSED, label

            # the framing door, which no composed request can reach: it is
            # the one most easily forgotten, and an unmarked answer here
            # would tell a client its command MIGHT have been admitted
            for line in ("{not json", '["a list, not an object"]'):
                answer = await _call_line(server.path, line)
                assert answer["ok"] is False, line
                assert answer["refused"] is True, line
                assert outcome_of(answer) == REFUSED, line

            # a decision that goes against the caller is NOT one of these: it
            # took an index, so it says which one, and never says `refused`
            lost = await _call(server.path, envelope(expect={"job:j": rev + 99}))
            assert lost["ok"] is False
            assert "refused" not in lost
            assert outcome_of(lost) == REJECTED
            assert isinstance(lost["index"], int)

            applied = await _call(server.path, envelope(request_id="sweep-3"))
            assert outcome_of(applied) == APPLIED
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_a_command_with_no_decision_is_the_only_unmarked_no(short_root: Path) -> None:
    """The uncertain answer is the one thing `refused` must never appear on,
    and it is what the absence of that flag is reserved to mean. A client
    that read it as a refusal would resend a command that may be applying."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        loop_task.cancel()  # the writer stops; the socket keeps serving
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task
        server.DECISION_TIMEOUT_S = 0.2
        try:
            read = await _call(server.path, {"cmd": "status", "job": "j", "v": PROTOCOL_VERSION})
            answer = await _call(
                server.path,
                {
                    "v": PROTOCOL_VERSION,
                    "cmd": "sendevent",
                    "baseline_id": read["baseline_id"],
                    "epoch": read["epoch"],
                    "request_id": "no-answer",
                    "verb": "ON_HOLD",
                    "payload": {"job": "j"},
                    "expect": {"job:j": read["jobs"]["j"]["state_rev"]},
                },
            )
            assert outcome_of(answer) == UNKNOWN
        finally:
            await server.close()
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())


def test_the_shell_spends_a_different_exit_code_on_each_outcome(short_root: Path) -> None:
    """A script's whole view of a command is its exit status, so the three
    failures have to be three of them: 2 says re-send, 3 says re-read, 4
    says look before you touch anything. Collapsing them into one nonzero
    would make the shell the one client that cannot act on the distinction
    the protocol exists to draw."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            applied = await asyncio.to_thread(_sendevent_cli, server.path, "ON_HOLD", "--job", "j")
            assert applied.returncode == 0, applied.stderr

            refused = await asyncio.to_thread(
                _sendevent_cli, server.path, "ON_HOLD", "--job", "ghost"
            )
            assert refused.returncode == 2
            assert json.loads(refused.stdout)["refused"] is True

            rejected = await asyncio.to_thread(
                _sendevent_cli, server.path, "OFF_HOLD", "--job", "j", "--expect", "0"
            )
            assert rejected.returncode == 3
            assert json.loads(rejected.stdout)["decision"] == "rejected"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_the_shell_is_told_the_id_that_makes_its_retry_safe(short_root: Path) -> None:
    """Exit 4 is the outcome whose recovery a shell cannot improvise. The
    engine can recognise a retry only by `request_id`, and the answer that
    would have carried this one never arrived -- so the CLI prints the id it
    sent, and takes it back through `--request-id`. Without both halves the
    only available retry is a NEW command, which is the double apply the
    dedup path exists to prevent."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        loop_task.cancel()  # nothing will be decided while the writer is down
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task
        server.DECISION_TIMEOUT_S = 0.2
        try:
            lost = await asyncio.to_thread(
                _sendevent_cli, server.path, "ON_HOLD", "--job", "j", "--request-id", "operator-1"
            )
            assert lost.returncode == 4
            assert "--request-id operator-1" in lost.stderr
            assert "no decision within" in json.loads(lost.stdout)["error"]
        finally:
            await server.close()
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())


def test_a_retry_under_the_original_id_applies_nothing_twice(short_root: Path) -> None:
    """The other half: an id carried back is answered from the ORIGINAL
    decision. Two invocations, one index, one journal record -- and the
    second answer is the first one, not a second decision that happened to
    agree."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            first = await asyncio.to_thread(
                _sendevent_cli,
                server.path,
                "CHANGE_STATUS",
                "--job",
                "j",
                "--status",
                "SUCCESS",
                "--request-id",
                "operator-2",
            )
            assert first.returncode == 0, first.stderr
            body = json.loads(first.stdout)
            run_after_first = engine.oracle.store.job["j"].run_number

            # the same command again, under the same id and the SAME expect
            # (the revision the first one was composed against, now stale --
            # which is what makes this a retry rather than a fresh command)
            retry = await asyncio.to_thread(
                _sendevent_cli,
                server.path,
                "CHANGE_STATUS",
                "--job",
                "j",
                "--status",
                "SUCCESS",
                "--expect",
                str(body["revisions"]["job:j"] - 1),
                "--request-id",
                "operator-2",
            )
            assert retry.returncode == 0, retry.stderr
            assert json.loads(retry.stdout)["index"] == body["index"]
            assert engine.oracle.store.job["j"].run_number == run_after_first
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())
    records = [
        r for r in read_journal(run_root / "journal.jsonl") if r.get("request_id") == "operator-2"
    ]
    # one admission and one decision -- the retry added neither
    assert [r["rec"] for r in records] == ["input", "decision"]


def test_cli_sendevent_exit_code_reaches_the_change_status_payload(short_root: Path) -> None:
    """--exit-code (cli_control.py) rides in the CHANGE_STATUS payload as an
    int, and the CLI's own exit code follows the answer exactly as it does
    for --status: 0 for applied, 3 once the same --expect is stale."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            applied = await asyncio.to_thread(
                _sendevent_cli,
                server.path,
                "CHANGE_STATUS",
                "--job",
                "j",
                "--status",
                "FAILURE",
                "--exit-code",
                "7",
            )
            assert applied.returncode == 0, applied.stderr
            assert json.loads(applied.stdout)["decision"] == "applied"
            assert engine.oracle.store.job["j"].exit_code == 7

            stale = await asyncio.to_thread(
                _sendevent_cli,
                server.path,
                "CHANGE_STATUS",
                "--job",
                "j",
                "--status",
                "SUCCESS",
                "--exit-code",
                "0",
                "--expect",
                "0",
            )
            assert stale.returncode == 3
            assert engine.oracle.store.job["j"].exit_code == 7  # unchanged
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_the_shell_can_read_the_revision_its_expect_has_to_name(short_root: Path) -> None:
    """The read half of ss0, at the shell. `global` was on the wire from
    S1c and reachable only from the TUI, which left `SET_GLOBAL` as the one
    mutation a script could not compose honestly: its precondition is a
    revision it had no verb to read. An unset name answers at 0 rather than
    vanishing, because 0 is what a conditional create locks against."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            before = await asyncio.to_thread(_query_cli, server.path, "global", "--name", "gate")
            assert before.returncode == 0, before.stderr
            assert json.loads(before.stdout)["globals"]["gate"] == {
                "present": False,
                "value": None,
                "state_rev": 0,
            }

            created = await asyncio.to_thread(
                _sendevent_cli, server.path, "SET_GLOBAL", "--global", "gate=open", "--expect", "0"
            )
            assert created.returncode == 0, created.stderr

            # the conditional create, run a second time against the same
            # "still absent" precondition: rejected, not silently overwritten
            again = await asyncio.to_thread(
                _sendevent_cli, server.path, "SET_GLOBAL", "--global", "gate=shut", "--expect", "0"
            )
            assert again.returncode == 3
            assert engine.oracle.store.globals_["gate"].value == "open"

            after = await asyncio.to_thread(
                _query_cli, server.path, "globals", "--name", "gate", "--name", "absent"
            )
            answers = json.loads(after.stdout)["globals"]
            assert answers["gate"]["present"] is True
            assert answers["gate"]["state_rev"] > 0
            assert answers["absent"] == {"present": False, "value": None, "state_rev": 0}
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_the_estate_skim_carries_the_revision_it_is_read_before_acting(short_root: Path) -> None:
    """`--brief` is what an operator reads immediately before deciding to
    act, so it is where the revision has to be: an `--expect` composed from
    a number this process fetched a moment ago names what the CLI saw, and
    one composed from the skim names what the OPERATOR saw."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        try:
            skim = await asyncio.to_thread(_query_cli, server.path, "status", "--brief")
            assert skim.returncode == 0, skim.stderr
            line = next(ln for ln in skim.stdout.splitlines() if ln.startswith("j "))
            rev = engine.oracle.store.job["j"].state_rev
            assert f"rev {rev}" in line
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_an_attempt_carries_its_envelope_into_the_log_and_back() -> None:
    """The record round-trips: what the caller named is what replay reads.
    Without this an admitted-but-undecided attempt would be re-gated against
    no precondition at all -- which is to say, applied unconditionally."""
    attempt = Attempt(
        index=4,
        at=T0,
        request_id="r4",
        fingerprint="f",
        kind="ON_HOLD",
        payload={"job": "j"},
        source="control",
        expect={"job:j": 12},
        epoch=0,
        claimed_actor="alice@host",
    )
    assert attempt.event() is not None
    assert attempt.expect == {"job:j": 12}
    with pytest.raises(ValueError, match="extra_forbidden|Extra inputs"):
        Attempt(index=1, at=T0, request_id="r", fingerprint="f", nonsense=1)  # type: ignore[call-arg]


# ------------------------------- the gates' own refusals and no-ops (DL-105)


def test_a_verb_that_addresses_nothing_is_refused_at_the_door() -> None:
    """ss6's `expect` names the ONE entity a verb addresses, so a verb that
    names no entity has no precondition it could carry. Refused where the
    envelope is parsed, not applied and then puzzled over -- and the message
    names the shape, because a caller who cannot see what was wrong retries
    the same thing."""
    with pytest.raises(EnvelopeError, match="SET_GLOBAL addresses a global by name"):
        addressed_key("SET_GLOBAL", {"value": "go"})
    with pytest.raises(EnvelopeError, match="SET_GLOBAL addresses a global by name"):
        addressed_key("SET_GLOBAL", {"name": ""})
    with pytest.raises(EnvelopeError, match="KILLJOB addresses a job by name"):
        addressed_key("KILLJOB", {"run_number": 3})
    with pytest.raises(EnvelopeError, match="KILLJOB addresses a job by name"):
        addressed_key("KILLJOB", {"job": 7})


def test_a_claimed_actor_that_is_not_a_string_is_refused() -> None:
    """It is a client HINT and it still has a type: the leader stamps it into
    the admitted record and the fingerprint hashes it, so a caller that sends
    a dict here would make its own retries un-dedupable."""
    request = _request(claimed_actor="alice@host")
    request["claimed_actor"] = {"name": "alice"}
    with pytest.raises(EnvelopeError, match="claimed_actor must be a string"):
        parse_envelope(request, addressed="job:j", baseline_id="the-log")


def test_an_attempt_carries_a_verb_or_a_host_command_never_both() -> None:
    """The two shapes of an admitted input are exclusive by construction
    (S5a): a host command carries no oracle event, and an attempt that
    claimed both would be fed AND applied to the routing table."""
    with pytest.raises(ValueError, match="never both"):
        Attempt(
            index=1,
            at=T0,
            request_id="r",
            fingerprint="f",
            kind="ON_HOLD",
            payload={"job": "j"},
            host=HostCommand(verb="drain", host_id="local"),
        )


def test_the_stale_gate_passes_a_completion_that_names_no_job() -> None:
    """The gate reads a completion's job to find the run it reports on. An
    event with none addresses nothing it could be stale about, so it passes
    -- refusing would turn a malformed report into a decision, and the gate
    is a precondition, not a validator."""
    oracle = Oracle(lower_source(_SOLO_JIL))
    assert stale_reason(oracle, Event(at=T0, kind="STATUS", payload={"status": "SUCCESS"})) is None


def test_the_stale_gate_rejects_a_completion_for_a_row_an_operator_set_inactive() -> None:
    """DL-235: an injected INACTIVE plans no KILL, so the process the operator
    reset runs on and its exit still reports in. The gate must reject it by
    name -- `rt.status` is read straight from the row the operator left --
    rather than let it rewrite INACTIVE back to SUCCESS or FAILURE."""
    oracle = Oracle(lower_source(_SOLO_JIL))
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
    assert (oracle.store.job["j"].status, oracle.store.job["j"].run_number) == ("RUNNING", 1)
    oracle.feed(Event(at=T0, kind="STATUS", payload={"job": "j", "status": "INACTIVE"}))
    run_number = oracle.store.job["j"].run_number
    ev = Event(at=T0, kind="STATUS", payload={"job": "j", "run_number": run_number, "exit_code": 0})
    assert stale_reason(oracle, ev) == "job not live: INACTIVE"


def test_the_stale_gate_passes_a_completion_for_a_starting_or_a_running_row() -> None:
    """The two LIVE statuses, both unrejected: a completion for a row the
    oracle still holds live is exactly what the gate exists to let through."""
    starting = Oracle(lower_source(_SOLO_JIL))
    starting.feed(Event(at=T0, kind="STATUS", payload={"job": "j", "status": "STARTING"}))
    assert starting.store.job["j"].status == "STARTING"
    run_number = starting.store.job["j"].run_number
    ev = Event(at=T0, kind="STATUS", payload={"job": "j", "run_number": run_number, "exit_code": 0})
    assert stale_reason(starting, ev) is None

    running = Oracle(lower_source(_SOLO_JIL))
    running.feed(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
    assert running.store.job["j"].status == "RUNNING"
    run_number = running.store.job["j"].run_number
    ev = Event(at=T0, kind="STATUS", payload={"job": "j", "run_number": run_number, "exit_code": 0})
    assert stale_reason(running, ev) is None


def test_the_stale_gate_still_names_a_terminal_row_already_terminal() -> None:
    """The pre-DL-235 reason is unchanged for the case it already covered --
    only the row a terminal status leaves behind is not LIVE either, but the
    more specific reason wins."""
    oracle = Oracle(lower_source(_SOLO_JIL))
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
    assert oracle.store.job["j"].status == "RUNNING"
    oracle.feed(Event(at=T0, kind="STATUS", payload={"job": "j", "status": "TERMINATED"}))
    run_number = oracle.store.job["j"].run_number
    ev = Event(at=T0, kind="STATUS", payload={"job": "j", "run_number": run_number, "exit_code": 0})
    assert stale_reason(oracle, ev) == "job already terminal"


def test_the_stale_gate_checks_run_number_before_liveness() -> None:
    """A moved run_number is refused on its own grounds even when the row it
    names is also not LIVE: "run_number mismatch" must not be shadowed by
    the new, more specific check that sits after it."""
    oracle = Oracle(lower_source(_SOLO_JIL))
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
    assert oracle.store.job["j"].status == "RUNNING"
    oracle.feed(Event(at=T0, kind="STATUS", payload={"job": "j", "status": "INACTIVE"}))
    stale_run_number = oracle.store.job["j"].run_number + 1
    ev = Event(
        at=T0,
        kind="STATUS",
        payload={"job": "j", "run_number": stale_run_number, "exit_code": 0},
    )
    assert stale_reason(oracle, ev) == "run_number mismatch"
