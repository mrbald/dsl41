"""Branch gaps of `runner_control.py` (DL-269).

Each test names the rule its branch implements (docs/control-protocol.md) and
asserts the branch's observable effect: an answer, a refusal and its message,
a state. Helpers come from `test_runner_control`; the access and boundary
fixtures come from the modules that own them.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import grp
import json
import os
import pwd
import socket as socket_mod
import sys
from pathlib import Path

import pytest
from test_access import _write_map
from test_boundary import C2_JIL, T0, _catalog, _close, _genesis, _request, _seal, _stage
from test_runner_control import (
    _body,
    _control_call,
    _fake_answer_server,
    _RawServer,
    _seal_wire,
    _sendevent,
    _serve,
    _teardown,
    _versioned,
    _wait_for_async,
)

import dsl41.runner_control as control_mod
from dsl41.attest import audit_period
from dsl41.boundary import EstateAnchor, default_anchor_dir
from dsl41.estate import roll_into_root
from dsl41.runner_access import AccessControl, PerimeterJournal, load_policy
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_control import (
    ControlClient,
    ControlClientError,
    ControlServer,
    StreamLineTooLong,
    claimed_actor,
    subscribe_lines,
)
from dsl41.runner_journal import read_journal
from dsl41.runner_startup import resume_run

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("unix-domain control sockets are POSIX-only", allow_module_level=True)

_ONE_JOB = "insert_job: s6b_job\njob_type: c\ncommand: x\nmachine: m1\n"


async def _open(server: ControlServer) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_unix_connection(str(server.path))


async def _ask(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request: dict) -> dict:
    writer.write(json.dumps(_versioned(request)).encode() + b"\n")
    await writer.drain()
    return json.loads(await asyncio.wait_for(reader.readline(), timeout=3.0))


async def _hang_up(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


# ------------------------------------------------- ss2 the socket lifecycle


def test_a_socket_that_cannot_bind_refuses_as_the_engine_error(short_root: Path) -> None:
    """ss2 socket lifecycle: two engines racing past the probe are separated
    by the bind, and a failed bind is a refusal of the engine's own class,
    not a raw OSError. A path whose directory does not exist fails the same
    bind. The server that did bind keeps answering."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _ONE_JOB)
        try:
            stranger = ControlServer(engine, short_root / "no-such-dir" / "control.sock")
            with pytest.raises(EngineError) as refused:
                await stranger.start()
            assert str(refused.value).startswith(f"cannot bind control socket {stranger.path}: ")
            assert stranger._server is None  # nothing was left bound
            answer = await _control_call(server.path, {"cmd": "status"})
            assert answer["ok"] is True
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def _group_this_process_is_not_in(directory: Path) -> grp.struct_group | None:
    # a socket takes its directory's group on some platforms (macOS), so that
    # group is not foreign either
    mine = {os.getegid(), *os.getgroups(), os.stat(directory).st_gid}
    return next((entry for entry in grp.getgrall() if entry.gr_gid not in mine), None)


def test_a_socket_group_the_process_cannot_grant_refuses_to_serve(short_root: Path) -> None:
    """access-model ss8: group reachability is deliberate, so a socket that
    cannot be handed to the policy's group refuses to serve rather than
    serving owner-only under a policy that promised the group. The role map
    is a valid one: the loader resolves the group and never chowns anything.

    The production path: an admin changes the run root's group to G, which
    the engine user is not in. On Linux the owner may chown a file to the gid
    it already has, so `AccessControl.arm` succeeds. The socket is created
    with the process's own group, so the chown to G fails with EPERM. The test
    builds the AccessControl directly because `arm` runs the same chown on the
    run root first and would refuse there. The granted twin is
    `test_access_socket_and_root_modes`."""
    foreign = _group_this_process_is_not_in(short_root)
    if os.geteuid() == 0 or foreign is None:  # pragma: no cover -- root may chown to any group
        pytest.skip("this process may chown to every group, or none is foreign")
    map_path = _write_map(
        short_root / "roles.toml",
        f'format_version = 1\nunmapped = "deny"\nsocket_group = "{foreign.gr_name}"\n',
    )
    policy = load_policy(map_path, generation=1)
    assert policy.socket_gid == foreign.gr_gid

    async def scenario() -> None:
        engine, plain, loop_task = await _serve(short_root / "run", _ONE_JOB)
        access = AccessControl(
            map_path=map_path, policy=policy, journal=PerimeterJournal(short_root / "p.jsonl")
        )
        armed = ControlServer(engine, short_root / "armed.sock", access=access)
        try:
            with pytest.raises(EngineError) as refused:
                await armed.start()
            assert str(refused.value).startswith(
                f"cannot arm control socket for group {foreign.gr_name!r}: "
            )
        finally:
            await armed.close()
            await _teardown(engine, plain, loop_task)

    asyncio.run(scenario())


# ------------------------------------------------ ss2 a handler that raises


def test_a_query_that_raises_answers_internal_error_and_the_stream_stays_in_sync(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss2: a handler that raises answers `internal error: ...` rather than
    dying unreplied -- a client must never see a bare timeout for a query
    bug -- and the answer is headerless. The next line on the same
    connection is answered normally once the fault is gone."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _ONE_JOB)
        reader, writer = await _open(server)
        try:
            healthy = await _ask(reader, writer, {"cmd": "status"})
            assert healthy["ok"] is True  # the non-triggering twin
            with monkeypatch.context() as fault:

                def boom() -> list:
                    raise RuntimeError("boom")

                fault.setattr(engine.oracle, "pending_timers", boom)
                broken = await _ask(reader, writer, {"cmd": "status"})
            assert broken == {
                "ok": False,
                "code": "internal_error",
                "error": "internal error: RuntimeError('boom')",
            }
            recovered = await _ask(reader, writer, {"cmd": "status"})
            assert recovered["ok"] is True
        finally:
            await _hang_up(writer)
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_a_client_that_vanishes_before_its_answer_costs_the_engine_nothing(
    short_root: Path,
) -> None:
    """A client that hangs up mid-exchange is its own problem (ss2): the
    handler ends without reporting an unhandled exception to the loop, the
    journal gains nothing, and the next client is answered."""

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        engine, server, loop_task = await _serve(short_root / "run", _ONE_JOB)
        try:
            before = engine.journal.path.read_bytes()
            reader, writer = await _open(server)

            async def accepted() -> bool:
                return len(server._conn_tasks) == 1

            await _wait_for_async(accepted)  # the handler is parked on its first read
            (handler,) = server._conn_tasks
            writer.write(json.dumps(_versioned({"cmd": "status"})).encode() + b"\n")
            await writer.drain()
            writer.transport.abort()  # closed with the request unanswered
            await asyncio.wait_for(asyncio.wait({handler}), timeout=3.0)
            assert handler.done() and handler.exception() is None
            await asyncio.sleep(0)  # the stream protocol reports a failed handler here
            assert unhandled == []
            assert engine.journal.path.read_bytes() == before
            assert (await _control_call(server.path, {"cmd": "status"}))["ok"] is True
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# --------------------------------------------------- ss3 the seal's answers


def test_a_seal_nothing_drains_is_unknown_and_a_second_one_is_refused(
    short_root: Path,
) -> None:
    """ss3 `seal`: a boundary that no loop drains answers `unknown` -- an
    `ok: false` with no `refused` -- and a timeout is never a refusal. The
    boundary is still in flight, so a second request is refused whole,
    naming the one ahead of it (`one seal at a time`). The healthy seal that
    answers inside its timeout is `test_pr30a_the_seal_verb_answers_before_the_engine_exits`."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _ONE_JOB)
        loop_task.cancel()  # the engine loop never drains the boundary
        server.SEAL_TIMEOUT_S = 0.2
        try:
            first = _body(
                await _control_call(server.path, _seal_wire(engine, request_id="r-first")),
                engine,
            )
            assert first == {
                "ok": False,
                "code": "seal_timeout",
                "error": "no boundary outcome within 0.2s: the seal may still commit --"
                " re-read before retrying, and retry only under request_id r-first",
            }
            second = _body(
                await _control_call(server.path, _seal_wire(engine, request_id="r-second")),
                engine,
            )
            assert second == {
                "ok": False,
                "code": "seal_in_flight",
                "refused": True,
                "error": "a boundary is already in flight (request_id r-first): one seal at a time",
            }
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_a_seal_reads_claimed_actor_as_a_string_or_not_at_all(short_root: Path) -> None:
    """ss3: `claimed_actor` is typed only when present, and absence is legal
    (it is defaulted). A number is refused before anything is parsed."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _ONE_JOB)
        try:
            refused = _body(
                await _control_call(server.path, _seal_wire(engine, claimed_actor=7)), engine
            )
            assert refused == {
                "ok": False,
                "code": "invalid_argument",
                "refused": True,
                "error": "malformed seal request: claimed_actor must be a string, got 7",
            }
            assert control_mod._seal_wire_error(_seal_wire(engine, claimed_actor=None)) is None
            assert control_mod._seal_wire_error(_seal_wire(engine)) is None
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------------------------------- ss3 framing refusals, per verb


def test_a_payload_that_is_not_an_object_is_refused_for_job_and_host_verbs(
    short_root: Path,
) -> None:
    """ss3: the payload is an object, whichever verb set names it. Nothing
    is admitted: the journal is byte-identical after both refusals."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _ONE_JOB)
        try:
            before = engine.journal.path.read_bytes()
            job = _body(
                await _control_call(
                    server.path, {"cmd": "sendevent", "verb": "STARTJOB", "payload": []}
                ),
                engine,
            )
            assert job == {
                "ok": False,
                "code": "invalid_argument",
                "refused": True,
                "error": "payload must be an object, got []",
            }
            host = _body(
                await _control_call(
                    server.path, {"cmd": "host", "verb": "drain", "payload": "local"}
                ),
                engine,
            )
            assert host == {
                "ok": False,
                "code": "invalid_argument",
                "refused": True,
                "error": "payload must be an object, got 'local'",
            }
            assert engine.journal.path.read_bytes() == before
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_a_host_id_with_an_unpaired_surrogate_is_refused_at_the_door(
    short_root: Path,
) -> None:
    """PR-10a, on the host verbs: a lone surrogate is a legal JSON escape
    and canonicalization raises on one, so the door refuses it and nothing
    is written. The same request with an ordinary non-ASCII id passes this
    check and meets the envelope instead."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _ONE_JOB)
        try:
            before = engine.journal.path.read_bytes()
            lone = _body(
                await _control_call(
                    server.path, {"cmd": "host", "verb": "drain", "payload": {"id": "\ud800"}}
                ),
                engine,
            )
            assert lone == {
                "ok": False,
                "code": "invalid_argument",
                "refused": True,
                "error": "host id carries an unpaired surrogate",
            }
            plain = await _control_call(
                server.path, {"cmd": "host", "verb": "drain", "payload": {"id": "hé"}}
            )
            assert plain["ok"] is False and "surrogate" not in plain["error"]
            assert engine.journal.path.read_bytes() == before
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_hosts_ids_are_a_list_of_strings_or_absent(short_root: Path) -> None:
    """ss4 `hosts [ids]`: `ids` is a list of host id strings. Anything else
    is refused; an empty list is a list and answers an empty table."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _ONE_JOB)
        try:
            for bad in (["local", 7], "local", {"local": 1}):
                refused = _body(
                    await _control_call(server.path, {"cmd": "hosts", "ids": bad}), engine
                )
                assert refused == {
                    "ok": False,
                    "code": "invalid_argument",
                    "error": "ids must be a list of host id strings",
                }
            empty = _body(await _control_call(server.path, {"cmd": "hosts", "ids": []}), engine)
            assert empty["ok"] is True and empty["hosts"] == {}
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_set_global_refuses_a_missing_name_and_a_non_string_value(short_root: Path) -> None:
    """ss3 framing: SET_GLOBAL names a global (a non-empty string) and gives
    it a string value. Both refusals admit nothing; the pair that is
    well-formed is admitted (`test_set_global_then_a_value_conditioned_job_fires`)."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _ONE_JOB)
        try:
            before = engine.journal.path.read_bytes()
            for name in ("", None, 5):
                refused = _body(
                    await _control_call(
                        server.path,
                        {
                            "cmd": "sendevent",
                            "verb": "SET_GLOBAL",
                            "payload": {"name": name, "value": "v"},
                        },
                    ),
                    engine,
                )
                assert refused == {
                    "ok": False,
                    "code": "invalid_argument",
                    "refused": True,
                    "error": "SET_GLOBAL requires a global name",
                }
            for value in (5, None, ["v"]):
                refused = _body(
                    await _control_call(
                        server.path,
                        {
                            "cmd": "sendevent",
                            "verb": "SET_GLOBAL",
                            "payload": {"name": "G", "value": value},
                        },
                    ),
                    engine,
                )
                assert refused == {
                    "ok": False,
                    "code": "invalid_argument",
                    "refused": True,
                    "error": "SET_GLOBAL requires a string value",
                }
            assert engine.journal.path.read_bytes() == before
            applied = await _sendevent(server.path, "SET_GLOBAL", name="G", value="v")
            assert applied["ok"] is True
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------------------------------------------ ss4 the query verbs


def test_explain_refuses_a_job_the_catalog_does_not_hold(short_root: Path) -> None:
    """ss4 `explain <job>`: an unknown job is refused with its name, and a
    request that names none is refused the same way. The known-job answer is
    `test_explain_null_condition_and_status_atom_truth_before_and_after`."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _ONE_JOB)
        try:
            unknown = await _control_call(server.path, {"cmd": "explain", "job": "nope"})
            assert _body(unknown, engine) == {
                "ok": False,
                "code": "unknown_job",
                "error": "unknown job 'nope'",
            }
            nameless = await _control_call(server.path, {"cmd": "explain"})
            assert _body(nameless, engine) == {
                "ok": False,
                "code": "unknown_job",
                "error": "unknown job None",
            }
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_explain_serves_a_globals_actual_value_beside_the_atoms_truth(
    short_root: Path,
) -> None:
    """DL-66: atom truth alone hides WHY, so a global atom carries the
    effective value (null = never set) beside its truth, and a job atom does
    not carry the field at all."""
    text = (
        "insert_job: s6b_up\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: s6b_dn\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(s6b_up) & v(S6B_FLAG) = go\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            before = await _control_call(server.path, {"cmd": "explain", "job": "s6b_dn"})
            assert before["atoms"] == [
                {"atom": "s(s6b_up)", "true": False},
                {"atom": "v(S6B_FLAG) = go", "true": False, "actual": None},
            ]
            assert (await _sendevent(server.path, "SET_GLOBAL", name="S6B_FLAG", value="go"))[
                "ok"
            ] is True

            async def seen() -> bool:
                r = await _control_call(server.path, {"cmd": "explain", "job": "s6b_dn"})
                return r["atoms"][1].get("actual") == "go"

            await _wait_for_async(seen)
            after = await _control_call(server.path, {"cmd": "explain", "job": "s6b_dn"})
            assert after["atoms"][1] == {"atom": "v(S6B_FLAG) = go", "true": True, "actual": "go"}
            assert "actual" not in after["atoms"][0]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_deps_names_a_cross_instance_atom_as_job_caret_instance(short_root: Path) -> None:
    """DL-65 `deps`: an upstream atom on another instance is the entity
    `JOB^INST`, never the bare job name -- the bare name is a different
    entity (SEM-07). The same-instance atom beside it keeps its bare name."""
    text = (
        "insert_xinst: PRD\nxtype: a\n\n"
        "insert_job: s6b_local\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: s6b_dep\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(s6b_local) & s(FEED^PRD, 9999)\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            deps = await _control_call(server.path, {"cmd": "deps", "job": "s6b_dep"})
            assert deps["ok"] is True
            assert deps["upstream"] == ["FEED^PRD", "s6b_local"]
            assert deps["globals"] == []
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_spec_drift_reads_an_estate_file_that_vanished_as_changed(short_root: Path) -> None:
    """DL-65 spec_drift: an unreadable or deleted input file counts as
    changed -- the running catalog can no longer be matched to its source.
    The flag is the sampled hint the lazy interval makes it, so the check is
    forced the way `test_status_spec_drift_false_then_true_on_rewrite_with_lazy_interval`
    forces it."""
    import hashlib

    estate = short_root / "estate.jil"
    estate.write_text(_ONE_JOB, encoding="utf-8")
    fingerprint = {str(estate): hashlib.sha256(estate.read_bytes()).hexdigest()}

    async def scenario() -> None:
        engine, server, loop_task = await _serve(
            short_root / "run", _ONE_JOB, estate_fingerprint=fingerprint
        )
        try:
            assert (await _control_call(server.path, {"cmd": "status"}))["spec_drift"] is False
            estate.unlink()
            server._drift_checked_at = None  # force the lazy re-check
            assert (await _control_call(server.path, {"cmd": "status"}))["spec_drift"] is True
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------------------------------------------- ss5 the live stream


async def _read_until(reader: asyncio.StreamReader, done) -> list[dict]:
    """Stream lines until `done(lines)` holds; loud on a quiet stream."""
    lines: list[dict] = []
    while not done(lines):
        raw = await asyncio.wait_for(reader.readline(), timeout=3.0)
        assert raw != b"", f"the stream ended after {lines}"
        lines.append(json.loads(raw))
    return lines


def test_a_cursor_ahead_of_the_log_is_owed_only_the_seqs_above_it(short_root: Path) -> None:
    """ss5: every seq'd record the stream owes is above the cursor, and the
    ack names it. A cursor the log has not reached yet is the case where the
    live half, not the backfill, holds the line: records at or below it are
    not sent, the first one above it is."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _ONE_JOB)
        reader, writer = await _open(server)
        try:
            cursor = engine.frontiers.committed_index + 2
            ack = await _ask(reader, writer, {"cmd": "subscribe", "since": cursor})
            assert ack == {"ok": True, "subscribed": True, "since": cursor}
            for verb in ("ON_HOLD", "OFF_HOLD", "ON_HOLD"):
                assert (await _sendevent(server.path, verb, job="s6b_job"))["ok"] is True
            lines = await _read_until(
                reader, lambda got: any(r.get("seq") == cursor + 1 for r in got)
            )
            # records are published in seq order, so the first seq'd line is
            # the first one the stream sent; the withheld ones are in the log
            assert [r["seq"] for r in lines if "seq" in r] == [cursor + 1]
            written = {r["seq"] for r in read_journal(engine.journal.path) if "seq" in r}
            assert set(range(1, cursor + 1)) <= written
        finally:
            await _hang_up(writer)
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


_DISPLACED = (
    "this engine can no longer prove it leads this estate's lineage: the anchor was deleted"
    " or replaced. Nothing is answered from a lineage this process does not lead"
    " (period-model ss1.3, PR-03)"
)


def test_pr03_a_live_record_is_not_published_once_the_lineage_is_displaced(
    short_root: Path,
) -> None:
    """PR-03 holds per RESPONSE at the live seam too: a record that was
    appended while the engine still led reaches a subscriber that is parked
    on its feed only through a lineage proof that is re-read first. The anchor
    lock is deleted in the same turn the second record is appended, so the
    handler wakes to a displaced leader: it sends the refusal, headerless,
    and hangs up. The first record, appended under an intact lineage, is
    streamed."""
    run_root = short_root / "run"
    engine = _genesis(run_root)

    async def scenario() -> None:
        server = ControlServer(engine, short_root / "c.sock")
        await server.start()
        reader, writer = await _open(server)
        try:
            ack = await _ask(reader, writer, {"cmd": "subscribe"})
            assert ack["ok"] is True and ack["subscribed"] is True
            assert engine.journal is not None
            engine.journal.preflight([])
            assert (await _read_until(reader, lambda got: len(got) == 1))[0]["rec"] == "preflight"
            engine.journal.preflight([])
            (default_anchor_dir(run_root) / "anchor.lock").unlink()  # displaced, same turn
            refusal = json.loads(await asyncio.wait_for(reader.readline(), timeout=3.0))
            assert refusal == {
                "ok": False,
                "code": "lineage_lost",
                "refused": True,
                "error": _DISPLACED,
            }
            assert await asyncio.wait_for(reader.readline(), timeout=3.0) == b""
        finally:
            await _hang_up(writer)
            await server.close()
            await engine.shutdown()

    asyncio.run(scenario())
    _close(engine)


def test_pr03_the_gap_marker_is_a_response_and_a_displaced_leader_sends_the_refusal(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss5 gap marker, PR-03: the marker is a RESPONSE, so the lineage proof
    runs in front of it like every other one. The root below owes the marker
    to a cursor under the closed period (the roll imports none of its WAL); the
    anchor lock is deleted while the backfill is read, and the subscriber is
    answered the refusal instead of a marker about an estate this process no
    longer leads. The marker's own answer is
    `test_pr49_a_cursor_below_the_earliest_retained_record_gets_the_gap_marker`."""
    root_a = short_root / "a"
    engine = _genesis(root_a)
    asyncio.run(_seal(engine, _request(engine, _stage(root_a, C2_JIL))))
    _close(engine)
    anchor_dir = default_anchor_dir(root_a)
    audit_period(root_a, 1, anchor=EstateAnchor(anchor_dir))
    catalog, _ = _catalog(C2_JIL)
    root_b = short_root / "b"
    roll_into_root(root_b, anchor_dir=anchor_dir, catalog_of=lambda _r, _m: catalog)
    rolled = asyncio.run(
        resume_run(
            catalog,
            root_b,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            anchor_dir=anchor_dir,
        )
    )
    real_read = control_mod.read_backfill

    def displaced_while_reading(wal: Path, *, since: int):
        backfill = real_read(wal, since=since)
        assert backfill.gap_from is not None  # the marker is owed to this cursor
        (anchor_dir / "anchor.lock").unlink()
        return backfill

    monkeypatch.setattr(control_mod, "read_backfill", displaced_while_reading)

    async def scenario() -> list[dict]:
        server = ControlServer(rolled, short_root / "c.sock")
        await server.start()
        reader, writer = await _open(server)
        try:
            ack = await _ask(reader, writer, {"cmd": "subscribe", "since": 0})
            assert ack["ok"] is True
            answer = json.loads(await asyncio.wait_for(reader.readline(), timeout=3.0))
            assert await asyncio.wait_for(reader.readline(), timeout=3.0) == b""
            return [answer]
        finally:
            await _hang_up(writer)
            await server.close()
            await rolled.shutdown()

    answered = asyncio.run(scenario())
    _close(rolled)
    assert answered == [{"ok": False, "code": "lineage_lost", "refused": True, "error": _DISPLACED}]


# ------------------------------------------------ ss6 what a client claims


def test_a_claimed_actor_falls_back_to_the_uid_when_the_host_has_no_name_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """concurrency-model ss6: the claim is a breadcrumb, and a container
    with no passwd entry for its uid still makes one (`uid<N>@host`). With a
    login name in the environment the claim names it."""
    for name in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.delenv(name, raising=False)
    host = socket_mod.gethostname()

    def no_entry(_uid: int):
        raise KeyError("getpwuid(): uid not found")

    with monkeypatch.context() as bare:
        bare.setattr(pwd, "getpwuid", no_entry)
        with pytest.raises((OSError, KeyError)):  # OSError from 3.13, KeyError before
            getpass.getuser()  # the premise: the host really has no name for it
        assert claimed_actor() == f"uid{os.getuid()}@{host}"
    monkeypatch.setenv("LOGNAME", "s6b-operator")
    assert claimed_actor() == f"s6b-operator@{host}"


# ------------------------------------------- ss6 the clients' own refusals


def test_a_reply_that_is_not_an_object_leaves_the_async_client_as_delivered(
    short_root: Path,
) -> None:
    """ss6 on the async transport (`roundtrip` has its own twin in
    `test_a_reply_that_is_not_an_object_was_still_delivered`): the engine
    answered, the answer is a list, the request reached it, and the
    connection is dropped so the next request starts clean."""

    async def scenario() -> None:
        path = short_root / "list.sock"
        server = await _fake_answer_server(path, b"[1, 2, 3]\n")
        client = ControlClient(path)
        try:
            with pytest.raises(ControlClientError) as caught:
                await client.request({"cmd": "status"})
            assert caught.value.delivered is True
            assert "response is not a JSON object" in str(caught.value)
            assert client._writer is None
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_subscribing_where_nothing_listens_is_an_undelivered_client_error(
    short_root: Path,
) -> None:
    """ss1: every failure this client meets leaves as ControlClientError.
    A connect that fails sent nothing, so it is not `delivered`."""

    async def scenario() -> None:
        client = ControlClient(short_root / "nobody.sock")
        with pytest.raises(ControlClientError) as caught:
            async for _record in client.subscribe():
                pass  # pragma: no cover -- nothing listens
        assert caught.value.delivered is False

    asyncio.run(scenario())


async def _scripted_server(path: Path, script: bytes) -> asyncio.Server:
    """Answer the subscribe request with `script`, then hang up."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(script)
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, path=str(path))


def test_a_stream_skips_a_torn_record_and_ends_quietly_when_the_engine_hangs_up(
    short_root: Path,
) -> None:
    """ss5/ss6: a torn record line is only a wake-up signal and is skipped,
    and the engine's hangup ends the iteration -- it is not an error, and the
    caller decides whether to resubscribe."""

    async def scenario() -> list[dict]:
        path = short_root / "torn.sock"
        script = b'{"ok": true, "subscribed": true, "since": 0}\n{"seq": 1\n{"seq": 2}\n'
        server = await _scripted_server(path, script)
        client = ControlClient(path)
        try:

            async def drain() -> list[dict]:
                return [record async for record in client.subscribe()]

            return await asyncio.wait_for(drain(), timeout=3.0)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) == [{"seq": 2}]


def test_an_ack_over_the_stream_limit_leaves_as_a_client_error(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss1: asyncio answers a line over the limit with ValueError, and the
    stream cannot resync past it, so it leaves as ControlClientError (DL-151).
    It is the plain class: `StreamLineTooLong` names a record line, and an ack
    is not one."""
    monkeypatch.setattr(control_mod, "SUBSCRIBER_BACKLOG_BYTES", 64)

    async def scenario() -> None:
        path = short_root / "long-ack.sock"
        server = await _scripted_server(path, b"x" * 4096)
        client = ControlClient(path)
        try:
            with pytest.raises(ControlClientError) as caught:
                async for _record in client.subscribe():
                    pass  # pragma: no cover -- the ack never arrives
            assert type(caught.value) is ControlClientError
            assert not isinstance(caught.value, StreamLineTooLong)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_a_subscribe_that_gets_no_ack_says_why(short_root: Path) -> None:
    """ss2/DL-151: no ack at all, and an ack the engine hung up in the middle
    of, are each a refusal with their own words -- not a stream that
    believes it is open."""

    async def refusal(script: bytes, name: str) -> str:
        path = short_root / name
        server = await _scripted_server(path, script)
        client = ControlClient(path)
        try:
            with pytest.raises(ControlClientError) as caught:
                async for _record in client.subscribe():
                    pass  # pragma: no cover -- no ack, no stream
            return str(caught.value)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    async def scenario() -> None:
        assert await refusal(b"", "none.sock") == "engine hung up before the subscribe ack"
        assert await refusal(b'{"ok": true, "subscribed"', "torn.sock") == (
            "the subscribe ack ended without a newline: read limit, or a hang-up mid-line"
        )

    asyncio.run(scenario())


def test_the_blocking_stream_yields_a_line_that_only_mentions_ok_unless_it_is_a_refusal(
    short_root: Path,
) -> None:
    """ss5: no record has a top-level `ok`, so only a line that spells one is
    parsed, and only `{"ok": false, ...}` ends the stream. A torn line that
    mentions `"ok"`, a non-object, and an `ok: true` object are all passed
    through. The refusal itself is
    `test_dl267_subscribe_lines_raises_the_engines_words_for_an_answer_line`."""
    ack = b'{"ok": true, "subscribed": true, "since": 0}\n'
    lines = [b'{"ok" torn\n', b'["ok"]\n', b'{"ok": true, "seq": 1}\n']
    path = short_root / "mention.sock"
    server = _RawServer(path, ack + b"".join(lines))
    try:
        stream = subscribe_lines(path, {"cmd": "subscribe", "v": 3})
        got = [next(stream) for _ in range(1 + len(lines))]
        stream.close()
    finally:
        server.close()
    assert got[1:] == [line.decode().rstrip("\n") for line in lines]
