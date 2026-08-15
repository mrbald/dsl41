"""Execution hosts: the routing table, drain, and eviction's gate (S5a).

Normative spec: docs/concurrency-model.md ss8 (host lifecycle), ss7
(quarantine and the barrier), ss3 (the state owner), ss6 (the envelope).
`ssN` in this file always names concurrency-model.

ss8 exists because ss7's quarantine is safe and insufficient on its own:
one dead host would hold its jobs forever. So the operator owns an explicit
routing state per host, durable in the log. Two obligations land here.

  * **CM-13, the drain.** `passive` routes nothing new and finishes what is
    running. Both halves are tested in one scenario, because either alone
    is easy and wrong: a drain that stopped running work is a kill, and a
    drain that kept routing is a no-op.
  * **CM-11's refusal half.** `evict` is the only state that lets another
    host run work bound to this one, so it is the only one that can cause a
    double run, and it is refused unless all three ss8 preconditions hold.
    The one S5a can prove today is the deadman: a host that runs none can
    never be evicted, because nothing bounds when its wrappers die.

The distinction that carries the rest is **refused vs rejected**
(control-protocol ss3). A malformed host verb never enters the log. A
failed ss8 precondition is a DECISION at an index: it reads mutable state,
so it can only be made where `expect` is made -- inside the input's own
batch, in log order -- and replay, which has no live host to probe, must
reach the same verdict from the same row. The last test in section 5 is
what makes that non-vacuous.

Held-ness is DERIVED throughout (`Engine.held_jobs`), never stored: S5c's
outbox is where intent becomes durable, and two records of one intent is
the parallel model DL-91 exists to catch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, HostRuntime, OracleError, RuntimeState
from dsl41.runner import Engine, resume_run, start_run
from dsl41.runner_adapters import FakeAdapter, SupervisorClient
from dsl41.runner_admission import PROTOCOL_VERSION, Envelope
from dsl41.runner_clock import RealClock, VirtualClock
from dsl41.runner_control import ControlServer, outcome_of, read_for, revision_in
from dsl41.runner_hosts import (
    LOCAL_EXECUTOR_ID,
    T_KILL_S,
    HostCommand,
    host_rejection_reason,
    routes_new_effects,
    seed_local_executor,
    skew_allowance,
)
from dsl41.runner_journal import read_journal

T0 = datetime(2026, 7, 1, 8, 0)

_SOLO_JIL = "insert_job: j\njob_type: c\ncommand: x\n"
_PAIR_JIL = "insert_job: a\njob_type: c\ncommand: x\n\ninsert_job: b\njob_type: c\ncommand: y\n"

#: what an engine's own executor sits at after genesis: one entity, one
#: seeding transaction, revision 1 -- the same rule a declared global follows.
SEEDED = 1


def _ev(kind: str, minutes: float, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)  # type: ignore[arg-type]


def _engine(jil: str = _SOLO_JIL, adapter: FakeAdapter | None = None) -> Engine:
    return Engine(
        lower_source(jil),
        clock=VirtualClock(start=T0),
        adapters={"CMD": adapter or FakeAdapter(default=None)},
    )


def _expect(request_id: str, revision: int, host_id: str = LOCAL_EXECUTOR_ID) -> Envelope:
    return Envelope(
        request_id=request_id, expect={RuntimeState.host_key(host_id): revision}, epoch=0
    )


def _cmd(verb: str, host_id: str = LOCAL_EXECUTOR_ID, force: bool = False) -> HostCommand:
    return HostCommand(verb=verb, host_id=host_id, force=force)  # type: ignore[arg-type]


def _quarantined(**fields: object) -> HostRuntime:
    """A row in the one state ss8 accepts as "unreachable from the leader".

    Built directly rather than reached through a verb, because its producer
    is S5d's unreachability detector. The gate is a pure function of a row,
    which is exactly what lets its bound be pinned before the thing that
    fills the row exists."""
    return HostRuntime(state="quarantined", **fields)  # type: ignore[arg-type]


def _table(**rows: HostRuntime) -> RuntimeState:
    store = RuntimeState()
    store.begin_input()
    for host_id, row in rows.items():
        store.register_host(host_id, deadman_s=row.deadman_s, at=row.last_contact)
        store.set_host_state(host_id, row.state)
    store.commit_input()
    return store


# ------------------------------------------------------- 1. the row and the owner


def test_the_routing_table_is_a_third_expect_namespace() -> None:
    """ss6's key space grows one namespace, not a parallel one (DL-93):
    durability, replay, the one-increment rule and the precondition check
    are machinery that already works on namespaced keys."""
    engine = _engine()
    store = engine.oracle.store
    assert RuntimeState.host_key("h2") == "host:h2"
    assert store.revision(f"host:{LOCAL_EXECUTOR_ID}") == SEEDED
    # absence you cannot name is absence you cannot lock against
    assert store.revision("host:never-registered") == 0
    assert store.host("never-registered") is None
    with pytest.raises(OracleError, match="unknown entity namespace"):
        store.revision("relay:h2")


def test_a_read_of_the_table_never_creates_a_row() -> None:
    """Unlike `runtime()`, which invents a job row on demand for the
    pseudo-entities the oracle addresses. A host the table does not know is
    one the ss7 barrier would never reconcile, so it must read as absent
    rather than spring into existence at its default `active`."""
    engine = _engine()
    assert engine.oracle.store.host("ghost") is None
    assert dict(engine.oracle.store.hosts) == {
        LOCAL_EXECUTOR_ID: engine.oracle.store.host(LOCAL_EXECUTOR_ID)
    }
    assert not routes_new_effects(engine.oracle.store.host("ghost"))


def test_cm02_a_host_command_moves_its_row_exactly_one_revision() -> None:
    """ss3's cardinality rule reaches the new entity for free, because the
    row is under the same owner. The second half is the half that matters:
    an input that changes nothing moves nothing, so a drain that is already
    a drain does not invalidate a precondition anyone was holding."""
    store = _table(h=HostRuntime())
    assert store.revision("host:h") == 1

    store.begin_input()
    store.set_host_state("h", "passive")
    store.set_host_state("h", "passive")  # twice, one input
    assert store.commit_input() == ["host:h"]
    assert store.revision("host:h") == 2

    store.begin_input()
    store.set_host_state("h", "passive")
    assert store.commit_input() == []
    assert store.revision("host:h") == 2


def test_a_re_registration_does_not_undo_a_drain() -> None:
    """ss8 makes the routing state durable precisely so a failover does not
    undo a drain -- and a relay that could undo one by re-registering would
    give back with one hand what that sentence takes with the other. What
    re-registration DOES refresh is identity: the deadman it now runs, and
    the contact it just made."""
    store = _table(h=HostRuntime())
    store.begin_input()
    store.set_host_state("h", "passive")
    store.commit_input()

    later = T0 + timedelta(minutes=5)
    store.begin_input()
    store.register_host("h", deadman_s=45.0, at=later)
    store.commit_input()

    row = store.host("h")
    assert row is not None
    assert row.state == "passive"  # the drain survived
    assert (row.deadman_s, row.last_contact) == (45.0, later)


def test_a_state_change_on_an_unregistered_host_is_a_loud_error() -> None:
    """The gate refuses these long before the owner sees one, so this is the
    backstop, not the message an operator reads. It exists because the
    alternative -- creating the row -- would put a host in the table that
    never registered, which is the one thing the table must not hold."""
    store = RuntimeState()
    store.begin_input()
    with pytest.raises(OracleError, match="no host 'nope'"):
        store.set_host_state("nope", "passive")
    with pytest.raises(OracleError, match="no host 'nope'"):
        store.evict_host("nope", forced_by=None)
    store.commit_input()


# ---------------------------------------------------- 2. the ss8 eviction gate


def test_cm11_a_host_with_no_deadman_can_never_be_evicted() -> None:
    """ss8 precondition 2, and the one S5a can prove on its own. The deadman
    is opt-in per run root because it costs something real -- tolerating an
    absent controller indefinitely is what lets an engine crash and resume
    with its runs intact (DL-79) -- and a run root without one is never
    reroutable except by force. No wait can substitute: nothing bounds when
    the wrappers die, so no elapsed time means anything."""
    long_gone = _table(h=_quarantined(last_contact=T0 - timedelta(days=365)))
    reason = host_rejection_reason(long_gone, _cmd("evict", "h"), T0)
    assert reason is not None and "runs no deadman" in reason
    # a year of silence does not help, which is the point of the test
    assert host_rejection_reason(long_gone, _cmd("evict", "h"), T0 + timedelta(days=365))


def test_cm11_eviction_is_refused_before_the_bound_and_permitted_after() -> None:
    """ss8 precondition 3. The bound is the deadman plus the time its exit
    takes to kill every wrapper, plus drift -- and the refusal reports the
    REMAINING wait, so the operator waits rather than guesses."""
    deadman = 60.0
    bound = deadman + T_KILL_S
    bound += skew_allowance(bound)
    store = _table(h=_quarantined(deadman_s=deadman, last_contact=T0))
    cmd = _cmd("evict", "h")

    early = host_rejection_reason(store, cmd, T0 + timedelta(seconds=bound - 10))
    assert early is not None and "wait 10.0s more" in early
    assert host_rejection_reason(store, cmd, T0 + timedelta(seconds=bound)) is not None
    assert host_rejection_reason(store, cmd, T0 + timedelta(seconds=bound + 0.5)) is None


def test_cm11_eviction_needs_the_leaders_own_record_of_unreachability() -> None:
    """ss8 precondition 1. `quarantined` IS that record: it is what the
    leader writes when a host stops answering, and reading a live probe here
    instead would make the gate decide differently on replay.

    A DRAIN is not that record and must not be read as one -- it asserts
    nothing about reachability, which is exactly why ss8 keeps the two
    states apart."""
    for state, marker in (("active", "is active"), ("passive", "is passive")):
        store = _table(h=HostRuntime(state=state, deadman_s=60.0, last_contact=T0))  # type: ignore[arg-type]
        reason = host_rejection_reason(store, _cmd("evict", "h"), T0 + timedelta(days=1))
        assert reason is not None and marker in reason and "precondition 1" in reason


def test_cm11_force_skips_the_preconditions_and_is_recorded_with_its_principal() -> None:
    """ss8: force is attributed, not forbidden. It exists because an
    operator with out-of-band knowledge -- the machine is powered off, the
    disk is gone -- is sometimes right, and waiting out a deadman is then
    pure loss. Loud, durable and attributable is the whole of its safety
    story, so the row keeps the claim rather than leaving it to a WAL grep.

    The contrast is the gated path, which records NO actor: there the
    preconditions are the justification, and naming a principal beside them
    would suggest the eviction rested on who asked."""
    store = _table(h=HostRuntime(state="active"))
    forced = _cmd("evict", "h", force=True)
    assert host_rejection_reason(store, forced, T0) is None

    store.begin_input()
    store.evict_host("h", forced_by="alice@ops")
    store.commit_input()
    row = store.host("h")
    assert row is not None
    assert (row.state, row.forced_by) == ("evicted", "alice@ops")
    # ss8: eviction bumps the fence a returning relay is checked against
    assert row.generation == 1


def test_an_evicted_host_is_not_evicted_again_and_not_activated_back() -> None:
    """Both refusals name what DOES clear the state, because a refusal an
    operator cannot act on is one they will route around. A returning relay
    re-registers at the new generation and self-fences first (CM-12, S5d);
    it is not an operator flipping a state back."""
    store = _table(h=HostRuntime())
    store.begin_input()
    store.evict_host("h", forced_by=None)
    store.commit_input()

    again = host_rejection_reason(store, _cmd("evict", "h", force=True), T0)
    assert again is not None and "already evicted, at generation 1" in again
    back = host_rejection_reason(store, _cmd("activate", "h"), T0)
    assert back is not None and "self-fencing" in back


def test_a_quarantined_host_refuses_an_operator_state_change() -> None:
    """The leader set it and the leader clears it. An operator activating a
    host that is not answering would only make the table lie -- and ss7
    holds its jobs meanwhile rather than rerouting them, which is what makes
    the lie expensive."""
    store = _table(h=_quarantined())
    for verb in ("activate", "drain"):
        reason = host_rejection_reason(store, _cmd(verb, "h"), T0)
        assert reason is not None and "quarantined" in reason


def test_addressing_a_host_that_is_not_in_the_table_is_a_rejection() -> None:
    """Not a refusal: whether a host exists is mutable state, so the verdict
    belongs where `expect`'s does. A host joins the table by registering,
    never by being addressed -- otherwise a typo would create one."""
    store = _table(h=HostRuntime())
    reason = host_rejection_reason(store, _cmd("drain", "typo"), T0)
    assert reason is not None and "no host 'typo' in the routing table" in reason


def test_the_routing_column_is_one_predicate() -> None:
    """ss8's table, second column. Only `active` routes new effects; the
    other three states differ in what happens to work already running, which
    is not this predicate's question."""
    routes = {
        state: routes_new_effects(HostRuntime(state=state))  # type: ignore[arg-type]
        for state in ("active", "passive", "quarantined", "evicted")
    }
    assert routes == {"active": True, "passive": False, "quarantined": False, "evicted": False}


# ------------------------------------------------------- 3. the drain (CM-13)


def test_cm13_a_drain_routes_nothing_new_and_finishes_what_is_running() -> None:
    """Both halves of CM-13 in one scenario, because either alone is easy
    and wrong: a drain that stopped running work would be a kill, and one
    that kept routing would be a no-op.

    `a` is already running when the drain lands and runs to SUCCESS through
    it. `b` starts afterwards: the oracle decides its start exactly as it
    always would -- a job's semantics do not depend on where its machine
    routes -- and the shell holds the spawn, so no process exists. Held, not
    failed and not rerouted: rerouting without proof the old executor is
    dead is the double run the whole model exists to prevent.

    `b` reads RUNNING because the oracle walks a start through STARTING to
    RUNNING inside one feed. That is what makes `held` worth publishing: a
    held job is indistinguishable from a working one by status alone."""
    engine = _engine(_PAIR_JIL, FakeAdapter({("a", 1): (60.0, 0)}, default=None))

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="a"))
        await engine.run_until_quiescent(T0 + timedelta(seconds=10))
        assert engine.live_jobs() == frozenset({"a"})

        drained = engine.submit_host(_cmd("drain"), _expect("r1", SEEDED))
        await engine.run_until_quiescent(T0 + timedelta(seconds=20))
        assert (await drained).decision == "applied"

        engine.inject(_ev("STARTJOB", 0.5, job="b"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))

        assert engine.oracle.store.job["b"].status == "RUNNING"  # the oracle started it
        assert engine.held_jobs() == frozenset({"b"})  # the shell did not
        assert engine.oracle.store.job["a"].status == "SUCCESS"  # and `a` ran through it
        assert engine.live_jobs() == frozenset()

    asyncio.run(scenario())


def test_activating_re_dispatches_what_the_drain_held() -> None:
    """ss8 calls `passive` reversible, and this is what reversible has to
    mean. Nothing else would ever start these jobs: the oracle decided their
    start once and will not decide it again, so an `activate` that left them
    STARTING forever would be a worse failure than the one draining
    avoids."""
    engine = _engine()

    async def scenario() -> None:
        drained = engine.submit_host(_cmd("drain"), _expect("r1", SEEDED))
        await engine.run_until_quiescent(T0 + timedelta(seconds=10))
        assert (await drained).decision == "applied"

        engine.inject(_ev("STARTJOB", 0.5, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.held_jobs() == frozenset({"j"})

        back = engine.submit_host(_cmd("activate"), _expect("r2", SEEDED + 1))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        assert (await back).decision == "applied"
        assert engine.held_jobs() == frozenset()
        assert engine.live_jobs() == frozenset({"j"})
        assert engine.oracle.store.job["j"].run_number == 1  # re-dispatched, not re-run

    asyncio.run(scenario())


def test_a_held_job_is_derived_from_state_the_engine_already_had() -> None:
    """No held set is stored, so nothing can fall out of step with the
    oracle. A job qualifies only if the oracle has it live at a run the
    shell never dispatched -- which is why a CHANGE_STATUS ghost does not
    qualify even while the host is drained: it advances no run_number, so
    the ghost-run gate had already refused it and it is inert, not waiting.

    Status alone would call it held, and would be wrong."""
    engine = _engine()

    async def scenario() -> None:
        drained = engine.submit_host(_cmd("drain"), _expect("r1", SEEDED))
        await engine.run_until_quiescent(T0 + timedelta(seconds=10))
        assert (await drained).decision == "applied"

        engine.inject(_ev("STATUS", 0.5, job="j", status="STARTING"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["j"].status == "STARTING"
        assert engine.oracle.store.job["j"].run_number == 0
        assert engine.held_jobs() == frozenset()

    asyncio.run(scenario())


# ------------------------------------------------------------- 4. the wire


@pytest.fixture
def short_root():
    """AF_UNIX paths are length-limited (104 bytes on macOS), so socket tests
    use a short base directory rather than pytest's deep tmp_path."""
    directory = tempfile.mkdtemp(prefix="dsl41h-", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def _call(sock_path: Path, request: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        writer.write(json.dumps(request).encode("utf-8") + b"\n")
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _serve(run_root: Path, text: str = _SOLO_JIL):
    engine = start_run(
        lower_source(text),
        run_root,
        clock=RealClock(),
        adapters={"CMD": FakeAdapter(default=None)},
        hold_open=True,
    )
    server = ControlServer(engine, run_root / "control.sock")
    await server.start()
    loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    return engine, server, loop_task


async def _teardown(engine: Engine, server: ControlServer, loop_task) -> None:
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
    await server.close()
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def _host_request(engine: Engine, **fields: object) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "cmd": "host",
        "baseline_id": engine.baseline_id,
        "epoch": 0,
        "request_id": "r1",
        "verb": "drain",
        "payload": {"id": LOCAL_EXECUTOR_ID},
        "expect": {f"host:{LOCAL_EXECUTOR_ID}": SEEDED},
    } | fields


def test_the_table_is_readable_and_an_absent_id_answers_at_revision_zero(
    short_root: Path,
) -> None:
    """The read a `host` command's `expect` is composed from (ss6). With no
    `ids` it answers the WHOLE table, unlike `globals`: ss7's barrier has to
    walk every host, so "everything" is a meaningful answer here."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        try:
            whole = await _call(server.path, {"cmd": "hosts", "v": PROTOCOL_VERSION})
            assert whole["ok"] is True
            assert whole["executor"] == LOCAL_EXECUTOR_ID
            assert whole["hosts"][LOCAL_EXECUTOR_ID] == {
                "present": True,
                "state": "active",
                "generation": 0,
                "deadman_s": None,
                "last_contact": whole["hosts"][LOCAL_EXECUTOR_ID]["last_contact"],
                "forced_by": None,
                "state_rev": SEEDED,
            }
            assert whole["baseline_id"] == engine.baseline_id  # the ss6 read header

            named = await _call(
                server.path, {"cmd": "hosts", "ids": ["nope"], "v": PROTOCOL_VERSION}
            )
            assert named["hosts"]["nope"] == {"present": False, "state_rev": 0}
            # and the client half reads a revision out of it without knowing how
            assert read_for("host:nope") == {"cmd": "hosts", "ids": ["nope"]}
            assert revision_in(named, "host:nope") == 0
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_a_host_command_meets_the_same_four_outcomes(short_root: Path) -> None:
    """control-protocol ss3's vocabulary is the envelope's, not one verb
    set's: ss0's mandate is on externally requested mutations, so a routing
    change names the revision it was composed against exactly like a kill,
    and gets one of the same four answers."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        try:
            applied = await _call(server.path, _host_request(engine))
            assert outcome_of(applied) == "applied"
            assert applied["kind"] == "drain"
            assert applied["revisions"] == {f"host:{LOCAL_EXECUTOR_ID}": SEEDED + 1}

            # rejected: the revision moved under a command composed at SEEDED
            stale = await _call(server.path, _host_request(engine, request_id="r2"))
            assert outcome_of(stale) == "rejected"
            assert "not the 1" in stale["error"]
            assert isinstance(stale["index"], int)  # it happened; it is in the log

            # refused: framing, and nothing reaches the log
            for spoiled in (
                {"verb": "obliterate"},
                {"payload": {"id": ""}},
                {"payload": {"id": LOCAL_EXECUTOR_ID, "force": "yes"}},
                {"expect": {"job:j": 1}},
            ):
                answer = await _call(server.path, _host_request(engine, request_id="rx", **spoiled))
                assert outcome_of(answer) == "refused", spoiled
            assert engine.oracle.store.revision(f"host:{LOCAL_EXECUTOR_ID}") == SEEDED + 1
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_a_host_command_that_names_no_revision_is_refused(short_root: Path) -> None:
    """ss0 admits one mandate, not one per verb set, so the same envelope
    parser refuses here -- which is the reason it is one function rather
    than a rule each transport re-implements."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        try:
            request = _host_request(engine)
            del request["expect"]
            answer = await _call(server.path, request)
            assert answer["refused"] is True
            assert f'{{"host:{LOCAL_EXECUTOR_ID}": N}}' in answer["error"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_status_publishes_a_held_job(short_root: Path) -> None:
    """A drained estate whose jobs sit in STARTING with no explanation is a
    silent hang, and a drain is an operation an operator has to be able to
    watch. Derived, never stored."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run")
        try:
            assert (await _call(server.path, _host_request(engine)))["ok"] is True
            start = await _call(
                server.path,
                {
                    "v": PROTOCOL_VERSION,
                    "cmd": "sendevent",
                    "baseline_id": engine.baseline_id,
                    "epoch": 0,
                    "request_id": "s1",
                    "verb": "STARTJOB",
                    "payload": {"job": "j"},
                    "expect": {
                        "job:j": revision_in(
                            await _call(server.path, {"cmd": "status", "job": "j", "v": 2}), "job:j"
                        )
                    },
                },
            )
            assert start["ok"] is True
            status = await _call(server.path, {"cmd": "status", "job": "j", "v": 2})
            # RUNNING, with no process: status alone cannot tell an operator
            # that, which is the whole reason `held` is published
            assert status["jobs"]["j"]["status"] == "RUNNING"
            assert status["jobs"]["j"]["held"] is True
            assert engine.live_jobs() == frozenset()
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------------------------------------ 5. durability across a restart


def test_a_drain_survives_a_resume(short_root: Path) -> None:
    """ss8's reason for putting the routing state in the log at all: a
    failover must not undo a drain. The engine seeds its own executor at
    genesis on both sides, so what the log has to carry is the STATE -- and
    the second engine holds the drained job exactly as the first did.

    The revision survives too, which is what makes an `expect` composed
    before the restart still mean something after it."""

    async def scenario() -> None:
        run_root = short_root / "run"
        engine, server, loop_task = await _serve(run_root)
        try:
            assert (await _call(server.path, _host_request(engine)))["ok"] is True
        finally:
            await _teardown(engine, server, loop_task)

        resumed = await resume_run(
            lower_source(_SOLO_JIL),
            run_root,
            clock=RealClock(),
            adapters={"CMD": FakeAdapter(default=None)},
            settle_seconds=0.0,
            grace_seconds=0.0,
        )
        try:
            row = resumed.oracle.store.host(LOCAL_EXECUTOR_ID)
            assert row is not None and row.state == "passive"
            assert resumed.oracle.store.revision(f"host:{LOCAL_EXECUTOR_ID}") == SEEDED + 1
            records = read_journal(run_root / "journal.jsonl")
            [drain] = [r for r in records if r.get("rec") == "host"]
            assert drain["host"] == {"verb": "drain", "id": LOCAL_EXECUTOR_ID, "force": False}
            assert drain["source"] == "control"
        finally:
            await resumed.shutdown()
            assert resumed.journal is not None
            resumed.journal.close()

    asyncio.run(scenario())


def test_a_held_job_survives_the_restart_the_drain_did(short_root: Path) -> None:
    """The other half of "a failover does not undo a drain", and the half
    that is easy to lose: the routing state survives on its own, but the
    work it was protecting goes through reconciliation, where a start with
    no spool trace is normally a crash between feed and spawn and is FAILED
    rather than silently re-run.

    On a host that routes nothing that inference is wrong -- there was no
    crash, the drain simply did its job -- so the job stays held, and the
    `activate` after the restart re-drives it at the same run_number. A
    drain whose state survived while its work was failed would be a drain in
    name only."""

    async def scenario() -> None:
        run_root = short_root / "run"
        engine, server, loop_task = await _serve(run_root)
        try:
            assert (await _call(server.path, _host_request(engine)))["ok"] is True
            started = await _call(
                server.path,
                {
                    "v": PROTOCOL_VERSION,
                    "cmd": "sendevent",
                    "baseline_id": engine.baseline_id,
                    "epoch": 0,
                    "request_id": "s1",
                    "verb": "STARTJOB",
                    "payload": {"job": "j"},
                    "expect": {"job:j": 0},
                },
            )
            assert started["ok"] is True
            assert engine.held_jobs() == frozenset({"j"})
        finally:
            await _teardown(engine, server, loop_task)

        resumed = await resume_run(
            lower_source(_SOLO_JIL),
            run_root,
            clock=RealClock(),
            adapters={"CMD": FakeAdapter(default=None)},
            settle_seconds=0.0,
            grace_seconds=0.0,
            hold_open=True,
        )
        # reconciliation has already run inside resume_run, so this is its
        # verdict, not a race with one
        assert resumed.oracle.store.job["j"].status == "RUNNING"  # not FAILURE
        assert resumed.held_jobs() == frozenset({"j"})

        loop_task = asyncio.ensure_future(resumed.run_until_quiescent(datetime.max))
        try:
            back = resumed.submit_host(_cmd("activate"), _expect("r9", SEEDED + 1))
            assert (await asyncio.wait_for(back, 5)).decision == "applied"
            # not FAILURE: reconciliation's "never spawned" verdict was
            # enqueued, not applied, at the point checked above -- running the
            # loop is what would have landed it
            assert resumed.oracle.store.job["j"].status == "RUNNING"
            assert resumed.live_jobs() == frozenset({"j"})
            assert resumed.oracle.store.job["j"].run_number == 1  # re-dispatched, not re-run
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
            await resumed.shutdown()
            assert resumed.journal is not None
            resumed.journal.close()

    asyncio.run(scenario())


def test_a_rejected_host_command_replays_as_the_rejection_it_was(short_root: Path) -> None:
    """What makes the gate's purity load-bearing rather than a stylistic
    claim. A durable decision is authoritative: the second engine reproduces
    the log's own history, not the history its gate would write today -- and
    since the ss8 gate reads a row rather than a live host, the two agree
    anyway. A gate that probed would not."""

    async def scenario() -> None:
        run_root = short_root / "run"
        engine, server, loop_task = await _serve(run_root)
        try:
            refused = await _call(server.path, _host_request(engine, verb="evict", request_id="e1"))
            assert outcome_of(refused) == "rejected"
            assert "precondition 1" in refused["error"]
        finally:
            await _teardown(engine, server, loop_task)

        resumed = await resume_run(
            lower_source(_SOLO_JIL),
            run_root,
            clock=RealClock(),
            adapters={"CMD": FakeAdapter(default=None)},
            settle_seconds=0.0,
            grace_seconds=0.0,
        )
        try:
            row = resumed.oracle.store.host(LOCAL_EXECUTOR_ID)
            assert row is not None
            assert (row.state, row.generation) == ("active", 0)  # the eviction never landed
            assert resumed.frontiers.applied_index == 1
        finally:
            await resumed.shutdown()
            assert resumed.journal is not None
            resumed.journal.close()

    asyncio.run(scenario())


# --------------------------------------------- 6. the deadman's half of the bound


def test_contact_refreshes_the_row_without_costing_a_revision_or_a_record() -> None:
    """S5b's design constraint, and the reason `last_contact` sits outside
    ss3's semantic projection.

    An engine renews its supervisor lease every twenty seconds. If that were
    an admitted input, every host row would move a revision three times a
    minute -- no operator could hold an `expect` on one, and the WAL would
    become a heartbeat log. Excluding it is safe in the only direction that
    matters: a FRESHER contact can only ever delay an eviction."""
    engine = _engine()
    store = engine.oracle.store
    before = store.host(LOCAL_EXECUTOR_ID)
    assert before is not None

    engine.note_executor_contact()
    engine.note_executor_contact()
    after = store.host(LOCAL_EXECUTOR_ID)
    assert after is not None
    assert after.last_contact == engine.clock.now()
    assert after.state_rev == before.state_rev == SEEDED  # no revision moved
    assert engine.frontiers.committed_index == 0  # and nothing was admitted

    # a host the table does not know is ignored, not created: contact with
    # something outside the inventory is not a registration
    store.touch_host("stranger", T0)
    assert store.host("stranger") is None


def test_the_recorded_deadman_is_what_the_supervisor_reports() -> None:
    """ss8's bound has to describe the HOST. A reattaching engine meets a
    supervisor it did not start -- possibly one launched with a different
    interval, or none -- so the row records what the supervisor says it runs,
    read back over the lease exchange, never the flag this engine was given.

    A wrong value here is not cosmetic: it is the length of the wait that
    stands between an operator and a double run."""
    client = SupervisorClient(Path("/nonexistent"), deadman_s=90.0)
    assert client.supervisor_deadman_s is None  # nothing said so yet

    client._note_contact({"ok": True, "deadman_s": 45.0})
    assert client.supervisor_deadman_s == 45.0  # the supervisor's, not the flag's
    client._note_contact({"ok": True})  # RENEW carries no interval
    assert client.supervisor_deadman_s == 45.0  # and does not erase one

    engine = Engine(
        lower_source(_SOLO_JIL),
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        deadman_s=client.supervisor_deadman_s,
    )
    row = engine.oracle.store.host(LOCAL_EXECUTOR_ID)
    assert row is not None and row.deadman_s == 45.0


def test_cm11_a_real_deadman_and_a_real_contact_make_the_bound_computable() -> None:
    """The two producers S5a was missing, joined to the gate S5a wrote. What
    is still synthetic here is `quarantined` -- ss8's first precondition, whose
    producer is S5d's unreachability detector -- so this pins the bound and
    not yet the whole eviction.

    The refusal names the remaining wait, because an operator who is told
    only "no" waits by guessing, and guessing short is the double run."""
    engine = Engine(
        lower_source(_SOLO_JIL),
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        deadman_s=60.0,
    )
    store = engine.oracle.store
    engine.note_executor_contact()  # last heard from at T0
    store.begin_input()
    store.set_host_state(LOCAL_EXECUTOR_ID, "quarantined")  # S5d's producer, by hand
    store.commit_input()

    cmd = _cmd("evict")
    bound = 60.0 + T_KILL_S
    bound += skew_allowance(bound)
    early = host_rejection_reason(store, cmd, T0 + timedelta(seconds=bound - 30))
    assert early is not None and "wait 30.0s more" in early and "deadman 60.0s" in early
    assert host_rejection_reason(store, cmd, T0 + timedelta(seconds=bound + 1)) is None

    # and a later contact pushes the bound out again -- the safe direction,
    # which is why refreshing it needs no input
    store.touch_host(LOCAL_EXECUTOR_ID, T0 + timedelta(seconds=bound))
    assert host_rejection_reason(store, cmd, T0 + timedelta(seconds=bound + 1)) is not None


# ------------------------------------------------------------- 7. the DL-93 pin


def test_the_oracle_never_names_a_host_row() -> None:
    """DL-93, as a check rather than a convention. A job's condition truth
    cannot depend on where its machine routes, so the interpreter must not
    be able to read the routing table -- and the DL-91 split is what makes
    that statically true: `HostRuntime` lives in the module `oracle.py`
    imports FROM, and `HOST` is not an `EventKind`."""
    import dsl41.oracle as oracle_module
    from dsl41.oracle_state import EventKind

    source = Path(oracle_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("HostRuntime", "host_key", "hosts", "routes_new_effects"):
        assert forbidden not in source, forbidden
    assert "HOST" not in EventKind.__args__  # type: ignore[attr-defined]


def test_the_engine_seeds_its_own_executor_without_spending_a_log_index() -> None:
    """The seed is genesis, not an admitted input -- the same device the
    catalog seed uses. Admitting it would spend an index per start recording
    a fact about how the process was launched, and would renumber every
    index in every journal that already exists."""
    engine = _engine()
    assert engine.executor_id == LOCAL_EXECUTOR_ID
    assert engine.frontiers.committed_index == 0
    assert routes_new_effects(engine.oracle.store.host(LOCAL_EXECUTOR_ID))

    # and it is the same function a bare replay reconstructs the table with
    oracle = Oracle(lower_source(_SOLO_JIL))
    assert oracle.store.host(LOCAL_EXECUTOR_ID) is None
    seed_local_executor(oracle.store, LOCAL_EXECUTOR_ID, at=T0)
    assert oracle.store.revision(f"host:{LOCAL_EXECUTOR_ID}") == SEEDED
