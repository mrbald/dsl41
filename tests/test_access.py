"""The access perimeter (docs/access-model.md ss12, DL-146).

The ss12 obligations, in order: zero-config unchanged (1), configured-
but-invalid refuses (2), resolution order (3), gate coverage and the
completeness gate (4), denial shape and receipts (5), the privileged
ledger (6), reload semantics and stream revocation (7), filesystem
modes (8), credential-less refusal (9), actor overwrite (10).

House style follows test_runner_control.py: real domain, asyncio.run
per scenario, short-path roots for AF_UNIX (sun_path limit).
"""

from __future__ import annotations

import asyncio
import contextlib
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import socket as socket_mod
import stat as stat_mod
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

from dsl41.ir import lower_source
from dsl41.runner import Engine
from dsl41.runner_access import (
    AccessControl,
    AccessError,
    Principal,
    Tier,
    load_policy,
    required_tier,
)
from dsl41.runner_access import REQUIRED_TIER
from dsl41.runner_admission import PROTOCOL_VERSION
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import RealClock
from dsl41.runner_control import ControlServer, command, read_for, revision_in
from dsl41.runner_journal import read_journal
from dsl41.runner_startup import start_run

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("unix-domain control sockets are POSIX-only", allow_module_level=True)

ME = pwd.getpwuid(os.geteuid()).pw_name
try:
    MY_GROUP: str | None = grp.getgrgid(os.getgid()).gr_name
except KeyError:  # pragma: no cover -- a container without the gid in /etc/group
    MY_GROUP = None


@pytest.fixture
def short_root():
    """Short-path base for AF_UNIX sockets (see test_runner_control)."""
    d = tempfile.mkdtemp(prefix="dsl41a-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _write_map(path: Path, body: str) -> Path:
    os.chmod(path.parent, 0o700)  # umask-proof: the loader checks the parent too
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _map_granting(path: Path, tier: str | None, *, unmapped: str = "deny") -> Path:
    rows = f'[[binding]]\nsubject = "user:os/{ME}"\ntier = "{tier}"\n' if tier is not None else ""
    return _write_map(path, f'format_version = 1\nunmapped = "{unmapped}"\n{rows}')


def _receipts(run_root: Path) -> list[dict]:
    path = run_root / "perimeter.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a healed torn tail leaves a blank or broken line
    return records


# ------------------------------------------------------------ the role map


def test_access_map_resolution_order(tmp_path: Path) -> None:
    """ss4/ss12.3: user row beats groups, highest group wins, unmapped
    last, realms never cross-match."""
    policy = load_policy(
        _write_map(
            tmp_path / "m.toml",
            'format_version = 1\nunmapped = "deny"\n'
            '[[binding]]\nsubject = "user:os/alice"\ntier = "read"\n'
            '[[binding]]\nsubject = "group:os/oncall"\ntier = "adm"\n'
            '[[binding]]\nsubject = "group:os/watchers"\ntier = "read"\n'
            '[[binding]]\nsubject = "group:corp/oncall"\ntier = "ops"\n',
        ),
        generation=1,
    )
    oncall = frozenset({"oncall", "watchers"})
    # the exact user row wins over an adm-granting group
    assert policy.resolve(Principal("os", "alice", oncall)) is Tier.READ
    # highest matching group wins
    assert policy.resolve(Principal("os", "bob", oncall)) is Tier.ADM
    assert policy.resolve(Principal("os", "bob", frozenset({"watchers"}))) is Tier.READ
    # unmapped -> deny
    assert policy.resolve(Principal("os", "bob", frozenset({"strangers"}))) is None
    # realms never cross-match: a corp bob in os-named groups gets corp rows only
    assert policy.resolve(Principal("corp", "bob", oncall)) is Tier.OPS
    assert policy.resolve(Principal("corp", "alice", frozenset())) is None


def test_access_map_unmapped_read_is_the_one_relaxation(tmp_path: Path) -> None:
    policy = load_policy(
        _write_map(tmp_path / "m.toml", 'format_version = 1\nunmapped = "read"\n'),
        generation=1,
    )
    assert policy.resolve(Principal("os", "nobody-in-particular", frozenset())) is Tier.READ


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('format_version = 2\nunmapped = "deny"\n', id="format-version"),
        pytest.param("format_version = true\n", id="format-version-bool"),
        pytest.param("format_version = 1.0\n", id="format-version-float"),
        pytest.param(
            'format_version = 1\n[[binding]]\nsubject = "user:os/a"\ntier = []\n',
            id="tier-not-a-string",
        ),
        pytest.param('format_version = 1\nunmapped = "adm"\n', id="unmapped-tier"),
        pytest.param("format_version = 1\nsurprise = true\n", id="unknown-key"),
        pytest.param(
            'format_version = 1\n[[binding]]\nsubject = "user:os/a"\ntier = "root"\n',
            id="unknown-tier",
        ),
        pytest.param(
            "format_version = 1\n"
            '[[binding]]\nsubject = "user:os/a"\ntier = "read"\n'
            '[[binding]]\nsubject = "user:os/a"\ntier = "ops"\n',
            id="duplicate-subject",
        ),
        pytest.param(
            'format_version = 1\n[[binding]]\nsubject = "user:os/*"\ntier = "read"\n',
            id="wildcard",
        ),
        pytest.param(
            'format_version = 1\n[[binding]]\nsubject = "user:alice"\ntier = "read"\n',
            id="no-realm",
        ),
        pytest.param(
            'format_version = 1\n[[binding]]\nsubject = "role:os/a"\ntier = "read"\n',
            id="bad-kind",
        ),
        pytest.param("not toml [", id="not-toml"),
    ],
)
def test_access_map_refuses_bad_bodies(tmp_path: Path, body: str) -> None:
    with pytest.raises(AccessError):
        load_policy(_write_map(tmp_path / "m.toml", body), generation=1)


def test_access_map_refuses_bad_files(tmp_path: Path) -> None:
    """ss4/ss12.2: missing, loose-mode, loose-parent and symlinked maps
    all refuse -- a configured path never falls back."""
    with pytest.raises(AccessError, match="cannot open"):
        load_policy(tmp_path / "absent.toml", generation=1)
    loose = _write_map(tmp_path / "loose.toml", "format_version = 1\n")
    os.chmod(loose, 0o664)
    with pytest.raises(AccessError, match="writable"):
        load_policy(loose, generation=1)
    target = _write_map(tmp_path / "target.toml", "format_version = 1\n")
    link = tmp_path / "link.toml"
    os.symlink(target, link)
    with pytest.raises(AccessError, match="cannot open"):
        load_policy(link, generation=1)
    nested = tmp_path / "shared"
    nested.mkdir()
    os.chmod(nested, 0o777)
    swap = nested / "m.toml"
    swap.write_text("format_version = 1\n")
    os.chmod(swap, 0o600)
    with pytest.raises(AccessError, match="parent"):
        load_policy(swap, generation=1)
    # a symlinked PARENT refuses too: O_NOFOLLOW guards only the last component
    realdir = tmp_path / "realdir"
    realdir.mkdir()
    os.chmod(realdir, 0o700)
    linked = _write_map(realdir / "m.toml", "format_version = 1\n")
    os.symlink(realdir, tmp_path / "dirlink")
    with pytest.raises(AccessError, match="parent directory is a symlink"):
        load_policy(tmp_path / "dirlink" / "m.toml", generation=1)
    assert linked.exists()  # the real file was never the problem


# ---------------------------------------------------------- the verb table


def test_access_verb_table_completeness_gate() -> None:
    """ss10/ss12.4: every cmd door has a row -- the `_dispatch` chain AND
    any pre-dispatch door in `_handle` (subscribe today). A new door in
    either fails here until it is classified; at runtime an unlisted cmd
    is default-denied, but only when access is armed, so the loud gate
    is this test."""
    import inspect

    from dsl41 import runner_control

    dispatch_src = inspect.getsource(runner_control.ControlServer._dispatch)
    dispatched = set(re.findall(r'cmd == "(\w+)"', dispatch_src))
    handle_src = inspect.getsource(runner_control.ControlServer._handle)
    doors = set(re.findall(r'get\("cmd"\) == "(\w+)"', handle_src))
    assert dispatched, "the completeness gate lost sight of the dispatcher"
    assert "subscribe" in doors, "the completeness gate lost sight of _handle's doors"
    assert dispatched | doors == set(REQUIRED_TIER)
    assert required_tier("no-such-cmd") is None
    assert required_tier(None) is None


def test_the_query_surface_forwards_exactly_the_read_verbs() -> None:
    """`cli_control._QUERY_VERBS` is DERIVED from the tier map (DL-152).

    It was a hand-written list of the READ half of `REQUIRED_TIER`, so a
    read verb added to the gate reached the gate, the help and the query
    surface at three different times. `hosts` is the one READ verb this
    surface does not forward -- `dsl41 control host list` owns it -- and
    that exclusion is the only thing the CLI still states."""
    from dsl41.cli_control import _QUERY_ELSEWHERE, _QUERY_VERBS
    from dsl41.runner_access import Tier

    # spelled out, not recomputed: a test that re-runs the source's own
    # comprehension passes for any tier map, including a wrong one
    assert _QUERY_VERBS == (
        "status",
        "trace",
        "explain",
        "spec",
        "deps",
        "timers",
        "plan",
        "global",
        "globals",
        "subscribe",
    )
    read_verbs = {verb for verb, tier in REQUIRED_TIER.items() if tier is Tier.READ}
    assert read_verbs == set(_QUERY_VERBS) | _QUERY_ELSEWHERE
    assert _QUERY_ELSEWHERE <= read_verbs, "an excluded verb must still BE a read verb"
    assert "hosts" not in _QUERY_VERBS


# ----------------------------------------------------------- the live gate


async def _serve_armed(
    run_root: Path, text: str, map_path: Path
) -> tuple[Engine, ControlServer, asyncio.Task, AccessControl]:
    catalog = lower_source(text)
    engine = start_run(
        catalog,
        run_root,
        clock=RealClock(),
        adapters={"CMD": FakeAdapter(), "FW": FakeAdapter()},
        hold_open=True,
    )
    access = AccessControl.arm(map_path, run_root)
    server = ControlServer(engine, run_root / "control.sock", access=access)
    await server.start()
    loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    return engine, server, loop_task, access


async def _teardown(engine: Engine, server: ControlServer, loop_task: asyncio.Task) -> None:
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
    await server.close()
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


async def _call(sock_path: Path, request: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        writer.write(json.dumps({"v": PROTOCOL_VERSION, **request}).encode() + b"\n")
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _envelope(verb: str, job: str, read: dict) -> dict:
    """One ss6 mutation envelope composed from a prior read, carrying a
    deliberately false wire claim (ss12.10 watches what lands)."""
    key = f"job:{job}"
    return command(
        verb,
        {"job": job},
        key=key,
        revision=revision_in(read, key),
        baseline_id=str(read.get("baseline_id") or ""),
        epoch=int(read.get("epoch") or 0),
        claimed_actor="impostor@nowhere",
    )


TEXT = "insert_job: acc_job\njob_type: c\ncommand: x\nmachine: m1\n"


def test_access_read_tier_queries_serve_and_mutations_refuse(short_root: Path) -> None:
    """ss5/ss12.5: a read principal queries; a mutation answers
    refused:true naming the tiers, consumes no engine index, reaches no
    WAL, and lands an access_denied receipt."""

    async def scenario() -> None:
        run_root = short_root / "run"
        map_path = _map_granting(short_root / "roles.toml", "read")
        engine, server, loop_task, _access = await _serve_armed(run_root, TEXT, map_path)
        try:
            status = await _call(server.path, read_for("job:acc_job"))
            assert status["ok"] is True
            index_before = engine.frontiers.applied_index
            wal_before = len(read_journal(run_root))
            denied = await _call(server.path, _envelope("STARTJOB", "acc_job", status))
            assert denied == {
                "ok": False,
                "refused": True,
                "error": f"os/{ME} holds read tier; sendevent:STARTJOB needs ops tier",
            }
            assert engine.frontiers.applied_index == index_before
            assert len(read_journal(run_root)) == wal_before
        finally:
            await _teardown(engine, server, loop_task)
        recs = _receipts(run_root)
        denials = [r for r in recs if r["rec"] == "access_denied"]
        assert len(denials) == 1
        assert denials[0]["principal"] == ME
        assert denials[0]["realm"] == "os"
        assert denials[0]["action"] == "sendevent:STARTJOB"
        assert denials[0]["required_tier"] == "ops"
        assert denials[0]["granted_tier"] == "read"
        assert denials[0]["policy_generation"] == 1

    asyncio.run(scenario())


def test_access_ops_admitted_actor_overwritten_and_ledgered(short_root: Path) -> None:
    """ss3/ss6/ss12.6/ss12.10: an ops principal mutates; the WAL records
    the authenticated spelling, not the wire claim; the admission lands
    in the privileged ledger."""

    async def scenario() -> None:
        run_root = short_root / "run"
        map_path = _map_granting(short_root / "roles.toml", "ops")
        engine, server, loop_task, _access = await _serve_armed(run_root, TEXT, map_path)
        try:
            status = await _call(server.path, read_for("job:acc_job"))
            admitted = await _call(server.path, _envelope("STARTJOB", "acc_job", status))
            assert admitted["ok"] is True, admitted
        finally:
            await _teardown(engine, server, loop_task)
        actors = {
            rec.get("claimed_actor") for rec in read_journal(run_root) if "claimed_actor" in rec
        }
        assert actors == {f"os/{ME}"}  # the impostor claim never lands (ss12.10)
        ledger = [r for r in _receipts(run_root) if r["rec"] == "privileged_admitted"]
        assert [r["action"] for r in ledger] == ["sendevent:STARTJOB"]
        assert ledger[0]["granted_tier"] == "ops"

    asyncio.run(scenario())


def test_access_unmapped_principal_denied_even_read(short_root: Path) -> None:
    """ss4: fail closed -- no binding, no unmapped relaxation, no reads."""

    async def scenario() -> None:
        run_root = short_root / "run"
        map_path = _map_granting(short_root / "roles.toml", None)  # no rows, deny
        engine, server, loop_task, _access = await _serve_armed(run_root, TEXT, map_path)
        try:
            denied = await _call(server.path, {"cmd": "status"})
            assert denied["refused"] is True
            assert "no tier" in denied["error"]
            # subscribe is a door of its own (it never reaches _dispatch)
            # and the gate covers it the same way
            sub = await _call(server.path, {"cmd": "subscribe"})
            assert sub["refused"] is True
            assert "subscribe needs read tier" in sub["error"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_access_reload_keeps_connection_swaps_policy(short_root: Path) -> None:
    """ss7/ss12.7: one connection, a mutation admitted under generation 1,
    the map edited to read, reload -- the same connection's next mutation
    refuses under generation 2. Receipts carry both generations."""

    async def scenario() -> None:
        run_root = short_root / "run"
        map_path = _map_granting(short_root / "roles.toml", "ops")
        engine, server, loop_task, access = await _serve_armed(run_root, TEXT, map_path)
        try:
            reader, writer = await asyncio.open_unix_connection(str(server.path))

            async def on_wire(request: dict) -> dict:
                writer.write(json.dumps({"v": PROTOCOL_VERSION, **request}).encode() + b"\n")
                await writer.drain()
                return json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))

            status = await on_wire(read_for("job:acc_job"))
            first = await on_wire(_envelope("STARTJOB", "acc_job", status))
            assert first["ok"] is True, first
            _map_granting(short_root / "roles.toml", "read")
            access.reload()
            second = await on_wire(_envelope("FORCE_STARTJOB", "acc_job", status))
            assert second["refused"] is True  # same connection, new policy
            assert "needs ops tier" in second["error"]
            still_reading = await on_wire({"cmd": "status", "job": "acc_job"})
            assert still_reading["ok"] is True
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        finally:
            await _teardown(engine, server, loop_task)
        loaded = [r for r in _receipts(run_root) if r["rec"] == "policy_loaded"]
        assert [r["generation"] for r in loaded] == [1, 2]

    asyncio.run(scenario())


def test_access_reload_invalid_map_keeps_old_policy(short_root: Path) -> None:
    """ss7/ss12.2: a bad candidate leaves the old policy serving and
    writes policy_reload_failed."""

    async def scenario() -> None:
        run_root = short_root / "run"
        map_path = _map_granting(short_root / "roles.toml", "ops")
        engine, server, loop_task, access = await _serve_armed(run_root, TEXT, map_path)
        try:
            map_path.write_text("format_version = 99\n")
            access.reload()
            status = await _call(server.path, {"cmd": "status", "job": "acc_job"})
            assert status["ok"] is True  # generation-1 ops grant still serves
        finally:
            await _teardown(engine, server, loop_task)
        recs = _receipts(run_root)
        assert [r["rec"] for r in recs if r["rec"].startswith("policy")] == [
            "policy_loaded",
            "policy_reload_failed",
        ]

    asyncio.run(scenario())


def test_access_reload_revokes_the_live_stream_that_lost_read(short_root: Path) -> None:
    """ss7/ss12.7, the live half: a subscribe stream whose principal
    loses read is closed with a stream_revoked receipt. The EXACTLY half
    -- a stream that keeps read survives -- needs a second principal,
    which a same-uid suite cannot host live; it is pinned at unit level
    in test_access_reload_receipt_gate_and_surviving_streams."""

    async def scenario() -> None:
        run_root = short_root / "run"
        map_path = _map_granting(short_root / "roles.toml", "read")
        engine, server, loop_task, access = await _serve_armed(run_root, TEXT, map_path)
        try:
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            writer.write(json.dumps({"v": PROTOCOL_VERSION, "cmd": "subscribe"}).encode() + b"\n")
            await writer.drain()
            # the ack is deterministic (DL-45) and registration precedes it
            ack = json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
            assert ack.get("subscribed") is True
            assert access.streams
            _map_granting(short_root / "roles.toml", None)  # deny-all
            access.reload()
            assert access.streams == {}
            # the server closed us: EOF arrives (torn tails are the transport's
            # business -- the receipt below is the assertion that matters)
            await asyncio.wait_for(reader.read(), timeout=3.0)
            # and the parked handler TASK ended too -- reload cancelled it;
            # a close alone would leave it on queue.get() forever
            deadline = time.monotonic() + 3.0
            while server._conn_tasks and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert not server._conn_tasks
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        finally:
            await _teardown(engine, server, loop_task)
        revoked = [r for r in _receipts(run_root) if r["rec"] == "stream_revoked"]
        assert [r["principal"] for r in revoked] == [ME]
        assert revoked[0]["action"] == "subscribe"
        assert revoked[0]["policy_digest"].startswith("sha256:")

    asyncio.run(scenario())


def test_access_no_resolvable_credential_refuses_connection(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss3/ss12.9: armed + no kernel credential = one refusal line, then
    the connection closes. The supervisor's permissive None is not here."""

    async def scenario() -> None:
        run_root = short_root / "run"
        map_path = _map_granting(short_root / "roles.toml", "adm")
        engine, server, loop_task, _access = await _serve_armed(run_root, TEXT, map_path)
        import dsl41.runner_control as rc

        monkeypatch.setattr(rc, "peer_principal", lambda sock: None)
        try:
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            line = json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
            assert line["refused"] is True
            assert "credential" in line["error"]
            # server closed the connection
            assert await asyncio.wait_for(reader.read(), timeout=2.0) == b""
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------------------------------------------- modes and startup


def test_access_socket_and_root_modes(short_root: Path) -> None:
    """ss8/ss12.8: armed with a socket_group -> root 0710 group-set,
    socket 0660 group-set, receipts 0600; the WAL and its friends stay
    owner-only."""
    if MY_GROUP is None:  # pragma: no cover -- gid missing from /etc/group
        pytest.skip("this process's gid has no name")

    async def scenario() -> None:
        run_root = short_root / "run"
        map_path = _write_map(
            short_root / "roles.toml",
            f'format_version = 1\nunmapped = "deny"\nsocket_group = "{MY_GROUP}"\n'
            f'[[binding]]\nsubject = "user:os/{ME}"\ntier = "read"\n',
        )
        engine, server, loop_task, _access = await _serve_armed(run_root, TEXT, map_path)
        try:
            gid = grp.getgrnam(MY_GROUP).gr_gid
            root_stat = os.stat(run_root)
            assert stat_mod.S_IMODE(root_stat.st_mode) == 0o710
            assert root_stat.st_gid == gid
            sock_stat = os.stat(server.path)
            assert stat_mod.S_IMODE(sock_stat.st_mode) == 0o660
            assert sock_stat.st_gid == gid
            assert stat_mod.S_IMODE(os.stat(run_root / "perimeter.jsonl").st_mode) == 0o600
            for entry in run_root.iterdir():  # ss8: nothing else opened up
                if entry == server.path:
                    continue
                assert stat_mod.S_IMODE(entry.stat().st_mode) & 0o077 == 0, entry
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_access_armed_owner_only_keeps_todays_modes(short_root: Path) -> None:
    """ss8/ss12.8: armed WITHOUT socket_group -- the owner-only armed
    mode. The gate and receipts are live; every mode stays as today
    (0700 root, 0600 socket, children untouched)."""
    # unit half, umask-proof: arm() without a group touches no mode at all
    unit_root = short_root / "unit"
    unit_root.mkdir(mode=0o700)
    child = unit_root / "logs"
    child.mkdir()
    os.chmod(child, 0o755)
    AccessControl.arm(_map_granting(short_root / "unit-roles.toml", "read"), unit_root)
    assert stat_mod.S_IMODE(os.stat(unit_root).st_mode) == 0o700
    assert stat_mod.S_IMODE(os.stat(child).st_mode) == 0o755

    async def scenario() -> None:
        run_root = short_root / "run"
        map_path = _map_granting(short_root / "roles.toml", "read")
        engine, server, loop_task, _access = await _serve_armed(run_root, TEXT, map_path)
        try:
            assert stat_mod.S_IMODE(os.stat(run_root).st_mode) == 0o700
            assert stat_mod.S_IMODE(os.stat(server.path).st_mode) == 0o600
            # the gate is live: an ops verb the read tier lacks refuses
            read = await _call(server.path, {"cmd": "status"})
            assert read["ok"] is True
            denied = await _call(server.path, _envelope("ON_HOLD", "acc_job", read))
            assert denied["ok"] is False and denied["refused"] is True
            # and the perimeter journal exists, owner-only
            receipts = run_root / "perimeter.jsonl"
            assert stat_mod.S_IMODE(os.stat(receipts).st_mode) == 0o600
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_access_zero_config_socket_stays_0600(short_root: Path) -> None:
    """ss4/ss12.1: no access -> 0600 socket, no perimeter journal, the
    wire claim passes through (the rest of obligation 1 is the whole
    existing suite, which runs access-free)."""

    async def scenario() -> None:
        run_root = short_root / "run"
        engine = start_run(
            lower_source(TEXT),
            run_root,
            clock=RealClock(),
            adapters={"CMD": FakeAdapter()},
            hold_open=True,
        )
        server = ControlServer(engine, run_root / "control.sock")
        await server.start()
        loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        try:
            assert stat_mod.S_IMODE(os.stat(server.path).st_mode) == 0o600
            status = await _call(server.path, read_for("job:acc_job"))
            admitted = await _call(server.path, _envelope("STARTJOB", "acc_job", status))
            assert admitted["ok"] is True
            assert not (run_root / "perimeter.jsonl").exists()
        finally:
            await _teardown(engine, server, loop_task)
        actors = {
            rec.get("claimed_actor") for rec in read_journal(run_root) if "claimed_actor" in rec
        }
        assert actors == {"impostor@nowhere"}  # byte-compatible passthrough

    asyncio.run(scenario())


def test_access_cli_run_refuses_a_configured_but_invalid_map(short_root: Path) -> None:
    """ss4/ss12.2 at the real door: `dsl41 run --access-map <absent>`
    refuses at startup, exit 2, naming the map."""
    estate = short_root / "estate.jil"
    estate.write_text(TEXT)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "dsl41",
            "run",
            str(estate),
            "--run-root",
            str(short_root / "run"),
            "--access-map",
            str(short_root / "absent.toml"),
            "--as-machine",
            "m1",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "access map" in proc.stderr
    assert "cannot open" in proc.stderr
    # the refusal wrote nothing: no claimed root, no anchor, no WAL
    assert not (short_root / "run").exists()
    assert not (short_root / "run.anchor").exists()


def test_access_fingerprint_carries_identity_never_tier(tmp_path: Path) -> None:
    """ss3/ss12.10: the seal fingerprint sees the actor SPELLING and
    nothing of policy. Held in three parts: the spelling is realm/name
    (groups out), the same request fingerprints identically across a
    reload that changes the principal's tier, and a DIFFERENT principal's
    retry mismatches by design (DL-144's stand-in refuses it)."""
    from dsl41.boundary import seal_fingerprint
    from dsl41.runner_ledger import STATE_MACHINE_VERSION
    from dsl41.seal import StagedNextPeriod

    a = Principal("os", "alice", frozenset({"oncall", "watchers"}))
    b = Principal("os", "alice", frozenset())
    assert a.spelling == b.spelling == "os/alice"

    staged = StagedNextPeriod(
        catalog_hash="sha256:" + "0" * 64,
        source_bundle_hash="sha256:" + "1" * 64,
        runtime_hash="sha256:" + "2" * 64,
        state_machine_version=STATE_MACHINE_VERSION,
    )

    def fingerprint(actor: str) -> str:
        return seal_fingerprint(
            source="request",
            baseline_id="b-1",
            epoch=1,
            next_period=staged,
            force_seal=False,
            claimed_actor=actor,
        )

    map_path = _map_granting(tmp_path / "roles.toml", "ops")
    run_root = tmp_path / "run"
    run_root.mkdir()
    access = AccessControl.arm(map_path, run_root)
    before = fingerprint(a.spelling)
    _map_granting(tmp_path / "roles.toml", "read")  # the role edit
    access.reload()
    assert access.policy.generation == 2
    assert fingerprint(a.spelling) == before  # retry survives the reload
    assert fingerprint("os/mallory") != before  # another identity mismatches


def _same_uid_socketpair() -> tuple[socket_mod.socket, socket_mod.socket]:
    return socket_mod.socketpair(socket_mod.AF_UNIX)


def test_access_peer_principal_resolves_this_process() -> None:
    """ss3: the kernel credential of a same-uid peer resolves to this
    user with OS groups attached."""
    from dsl41.runner_access import peer_principal

    left, right = _same_uid_socketpair()
    try:
        principal = peer_principal(left)
    finally:
        left.close()
        right.close()
    assert principal is not None
    assert principal.realm == "os"
    assert principal.name == ME
    assert MY_GROUP in principal.groups


# ----------------------------------------------- fail-closed branch pins


def test_access_map_more_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ss4: the loader's remaining refusal arms -- each one is a
    fail-closed decision and the gate holds every arm (DL-105's argument,
    applied to the perimeter)."""
    with pytest.raises(AccessError, match="cannot stat parent"):
        load_policy(tmp_path / "no-such-dir" / "m.toml", generation=1)
    with pytest.raises(AccessError, match="socket_group"):
        load_policy(
            _write_map(tmp_path / "sg.toml", "format_version = 1\nsocket_group = 7\n"),
            generation=1,
        )
    with pytest.raises(AccessError, match="array of tables"):
        load_policy(
            _write_map(tmp_path / "ba.toml", 'format_version = 1\nbinding = "x"\n'),
            generation=1,
        )
    with pytest.raises(AccessError, match="exactly subject and tier"):
        load_policy(
            _write_map(
                tmp_path / "bk.toml",
                'format_version = 1\n[[binding]]\nsubject = "user:os/a"\n',
            ),
            generation=1,
        )
    # a parent directory owned by someone else refuses (every geteuid
    # answer is shifted, so the parent check trips first)
    real = os.geteuid()
    import dsl41.runner_access as ra

    ok = _write_map(tmp_path / "own.toml", "format_version = 1\n")
    monkeypatch.setattr(ra.os, "geteuid", lambda: real + 1)
    with pytest.raises(AccessError, match="parent directory owned by"):
        load_policy(ok, generation=1)
    # a map FILE owned by someone else refuses (geteuid answers the real
    # uid for the parent check, then a shifted one for the file check --
    # the loader's call order is parent first, file second)
    answers = iter([real, real + 1])
    monkeypatch.setattr(ra.os, "geteuid", lambda: next(answers))
    with pytest.raises(AccessError, match="owned by uid"):
        load_policy(ok, generation=1)


def test_access_peer_principal_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """ss3: no kernel credential, or one that resolves to no passwd
    entry, is None -- the caller refuses on None."""
    import dsl41.runner_access as ra

    left, right = _same_uid_socketpair()
    try:
        monkeypatch.setattr(ra, "peer_uid", lambda sock: None)
        assert ra.peer_principal(left) is None
        monkeypatch.setattr(ra, "peer_uid", lambda sock: os.geteuid())

        def no_entry(uid: int):
            raise KeyError(uid)

        monkeypatch.setattr(ra.pwd, "getpwuid", no_entry)
        assert ra.peer_principal(left) is None
    finally:
        left.close()
        right.close()


def test_access_peer_principal_skips_nameless_gids(monkeypatch: pytest.MonkeyPatch) -> None:
    """ss3: a gid with no name cannot match a named row; it is skipped,
    not fatal."""
    import dsl41.runner_access as ra

    def nameless(gid: int):
        raise KeyError(gid)

    monkeypatch.setattr(ra.grp, "getgrgid", nameless)
    left, right = _same_uid_socketpair()
    try:
        principal = ra.peer_principal(left)
    finally:
        left.close()
        right.close()
    assert principal is not None
    assert principal.groups == frozenset()


def test_access_perimeter_journal_storage_failure_reports_false(tmp_path: Path) -> None:
    """ss6: the write answers False on a storage failure -- a denial
    still stands, an install does not happen."""
    from dsl41.runner_access import PerimeterJournal

    journal = PerimeterJournal(tmp_path / "no-such-dir" / "perimeter.jsonl")
    assert journal.write("access_denied", sync=True, principal="x") is False


def test_access_arm_refuses_when_the_receipt_cannot_land(tmp_path: Path) -> None:
    """ss7 at startup: an arming receipt that cannot be synced refuses
    the whole perimeter."""
    map_path = _map_granting(tmp_path / "roles.toml", "read")
    run_root = tmp_path / "run"
    run_root.mkdir()
    os.chmod(run_root, 0o500)  # journal cannot be created
    try:
        with pytest.raises(AccessError, match="arming receipt"):
            AccessControl.arm(map_path, run_root)
    finally:
        os.chmod(run_root, 0o700)


def test_access_an_unknown_socket_group_is_one_load_refusal(tmp_path: Path) -> None:
    """ss4/ss8: `socket_group` is resolved ONCE, by the loader (DL-152).

    Three sites looked the group up -- the CLI preflight, arming, and the
    control socket's bind -- so an unknown group was a different sentence
    at each and the preflight existed only to make one of them arrive
    early. The loader refuses it like any other unusable field, and every
    reader of a loaded policy takes the resolved gid."""
    map_path = _write_map(
        tmp_path / "roles.toml",
        'format_version = 1\nsocket_group = "dsl41-no-such-group-zz"\n',
    )
    with pytest.raises(AccessError, match="is not a group this host knows"):
        load_policy(map_path, generation=1)
    run_root = tmp_path / "run"
    run_root.mkdir()
    # arming LOADS, so it refuses in the loader's words
    with pytest.raises(AccessError, match="is not a group this host knows"):
        AccessControl.arm(map_path, run_root)


def test_access_a_known_socket_group_resolves_onto_the_policy(tmp_path: Path) -> None:
    """The resolved gid rides on `Policy`, and is None exactly when
    `socket_group` is (DL-152)."""
    own = grp.getgrgid(os.getgid())
    named = load_policy(
        _write_map(tmp_path / "g.toml", f'format_version = 1\nsocket_group = "{own.gr_name}"\n'),
        generation=1,
    )
    assert named.socket_group == own.gr_name and named.socket_gid == own.gr_gid
    bare = load_policy(_write_map(tmp_path / "b.toml", "format_version = 1\n"), generation=1)
    assert bare.socket_group is None and bare.socket_gid is None


class _RaisingWriter:
    """A dead transport: close() raises, which a revocation absorbs."""

    def close(self) -> None:
        raise RuntimeError("transport already gone")


def test_access_reload_receipt_gate_and_surviving_streams(tmp_path: Path) -> None:
    """ss7: (a) a policy_loaded receipt that cannot land keeps the OLD
    snapshot active; (b) on a successful reload, a stream that KEEPS
    read survives while the one that lost it is revoked even when its
    transport is already dead."""
    map_path = _map_granting(tmp_path / "roles.toml", "ops")
    run_root = tmp_path / "run"
    run_root.mkdir()
    access = AccessControl.arm(map_path, run_root)

    # (a) the receipt gate: journal path dies, reload keeps generation 1
    good_journal_path = access.journal.path
    access.journal.path = tmp_path / "no-such-dir" / "perimeter.jsonl"
    _map_granting(tmp_path / "roles.toml", "read")
    access.reload()
    assert access.policy.generation == 1
    assert access.policy.resolve(Principal("os", ME, frozenset())) is Tier.OPS

    # (b) receipts restored: the survivor keeps its stream, the loser's
    # dead transport still revokes cleanly
    access.journal.path = good_journal_path
    survivor = object()
    access.streams = {
        survivor: (Principal("os", ME, frozenset()), None),
        _RaisingWriter(): (Principal("os", "someone-else", frozenset()), None),
    }
    access.reload()
    assert access.policy.generation == 2
    assert list(access.streams) == [survivor]
    revoked = [
        json.loads(line)
        for line in good_journal_path.read_text().splitlines()
        if json.loads(line)["rec"] == "stream_revoked"
    ]
    assert [r["principal"] for r in revoked] == ["someone-else"]


def test_access_perimeter_seq_survives_restart(tmp_path: Path) -> None:
    """ss6: access_seq is an audit KEY; a resumed engine continues the
    series (a reissued 1 is a duplicate record by identity). A torn tail
    is skipped, not fatal."""
    from dsl41.runner_access import PerimeterJournal

    path = tmp_path / "perimeter.jsonl"
    first = PerimeterJournal(path)
    assert first.write("policy_loaded", sync=True, generation=1) is True
    assert first.write("access_denied", sync=True, principal="x") is True
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"rec": "acc')  # the torn tail of a crashed writer
    resumed = PerimeterJournal(path)
    assert resumed.write("policy_loaded", sync=True, generation=2) is True
    seqs = []
    for line in path.read_text().splitlines():
        try:
            seqs.append(json.loads(line)["access_seq"])
        except json.JSONDecodeError:
            continue  # the torn line stays torn; the writer appended PAST it
    assert seqs == [1, 2, 3]


def test_access_reload_fsync_failure_names_the_orphaned_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss7: when the policy_loaded LINE lands but its fsync fails, the
    old policy stays active and the failure receipt names the generation
    the orphaned line must not stand for."""
    import dsl41.runner_access as ra

    map_path = _map_granting(tmp_path / "roles.toml", "ops")
    run_root = tmp_path / "run"
    run_root.mkdir()
    access = AccessControl.arm(map_path, run_root)
    _map_granting(tmp_path / "roles.toml", "read")

    real_fsync = os.fsync

    def failing_fsync(fd: int) -> None:
        raise OSError("disk said no")

    monkeypatch.setattr(ra.os, "fsync", failing_fsync)
    access.reload()
    monkeypatch.setattr(ra.os, "fsync", real_fsync)
    assert access.policy.generation == 1  # the old snapshot stayed active
    recs = _receipts(run_root)
    # the orphaned policy_loaded line LANDED (write-then-fsync order) and
    # the failure receipt after it names the generation it voids
    kinds = [r["rec"] for r in recs]
    assert kinds == ["policy_loaded", "policy_loaded", "policy_reload_failed"]
    assert recs[2]["orphaned_generation"] == 2
    assert recs[2]["generation"] == 1


def test_access_sighup_reloads_the_live_engine(short_root: Path) -> None:
    """ss7/ss12.7 at the real door (the DL-105 shape: the wiring, not
    just the function): a running `dsl41 run --access-map` engine takes
    SIGHUP and lands the generation-2 policy_loaded receipt."""
    import signal as signal_mod

    estate = short_root / "estate.jil"
    estate.write_text(TEXT)
    run_root = short_root / "run"
    map_path = _map_granting(short_root / "roles.toml", "ops")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dsl41",
            "run",
            str(estate),
            "--run-root",
            str(run_root),
            "--access-map",
            str(map_path),
            "--as-machine",
            "m1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if (run_root / "control.sock").exists():
                break
            if proc.poll() is not None:
                assert proc.stderr is not None
                raise AssertionError(f"engine died: {proc.returncode}\n{proc.stderr.read()}")
            time.sleep(0.05)
        else:
            raise AssertionError("engine never served its socket")
        _map_granting(short_root / "roles.toml", "read")
        proc.send_signal(signal_mod.SIGHUP)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            loaded = [r for r in _receipts(run_root) if r["rec"] == "policy_loaded"]
            if [r["generation"] for r in loaded] == [1, 2]:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"no generation-2 receipt: {_receipts(run_root)}")
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def test_access_seq_recovery_arms(tmp_path: Path) -> None:
    """ss6, the recovery corners: a healed file recovers through its
    newline-terminated tail; a record without access_seq is stepped
    over; a journal of pure wreckage starts at 0; a heal that cannot
    write is not fatal."""
    from dsl41.runner_access import PerimeterJournal

    path = tmp_path / "p.jsonl"
    journal = PerimeterJournal(path)
    assert journal.write("policy_loaded", sync=False, generation=1) is True
    # a complete, newline-terminated file: recovery reads the tail directly
    assert PerimeterJournal(path)._seq == 1
    # a trailing valid-JSON line WITHOUT access_seq is stepped over
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"note": "no seq here"}\n')
    assert PerimeterJournal(path)._seq == 1
    # pure wreckage starts over at 0
    wreck = tmp_path / "wreck.jsonl"
    wreck.write_text('{"torn": "ta')
    assert PerimeterJournal(wreck)._seq == 0
    # a heal that cannot write is swallowed; recovery still answers
    frozen = tmp_path / "frozen.jsonl"
    frozen.write_text('{"access_seq": 7, "rec": "access_denied"}\n{"torn')
    os.chmod(frozen, 0o400)
    try:
        assert PerimeterJournal(frozen)._seq == 7
    finally:
        os.chmod(frozen, 0o600)


def test_access_reload_refuses_a_socket_group_change(tmp_path: Path) -> None:
    """ss8: the kernel half of the grant is applied at arming; a reload
    that names a different group is refused whole."""
    map_path = _map_granting(tmp_path / "roles.toml", "ops")
    run_root = tmp_path / "run"
    run_root.mkdir()
    access = AccessControl.arm(map_path, run_root)
    assert MY_GROUP is not None
    _write_map(
        tmp_path / "roles.toml",
        f'format_version = 1\nunmapped = "deny"\nsocket_group = "{MY_GROUP}"\n',
    )
    access.reload()
    assert access.policy.generation == 1
    assert access.policy.socket_group is None
    failures = [r for r in _receipts(run_root) if r["rec"] == "policy_reload_failed"]
    assert "fixed at arming" in failures[0]["error"]


def test_access_arming_chown_failure_is_one_named_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss8: a run root the kernel will not open to the socket group is an
    arming refusal that names the group and the cause, never a bare
    OSError. The probe that used to exercise this branch went with DL-152's
    preflight deletion; this is its witness now."""
    assert MY_GROUP is not None
    map_path = _write_map(
        tmp_path / "roles.toml",
        f'format_version = 1\nunmapped = "deny"\nsocket_group = "{MY_GROUP}"\n',
    )
    run_root = tmp_path / "run"
    run_root.mkdir()

    def _refuse(path: object, uid: int, gid: int) -> None:
        raise OSError("kernel says no")

    monkeypatch.setattr("dsl41.runner_access.os.chown", _refuse)
    with pytest.raises(AccessError, match="cannot open the run root to group"):
        AccessControl.arm(map_path, run_root)


def test_access_reload_refuses_a_group_that_kept_its_name_and_changed_its_gid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss8/DL-152: the gate compares the NAME and the GID. The chown at
    arming used a number; a host that re-numbered the group between arming
    and a reload would otherwise install a policy naming a gid nothing was
    chowned to, and the map would say a grant the kernel does not hold."""
    if MY_GROUP is None:  # pragma: no cover -- gid missing from /etc/group
        pytest.skip("no group name for this gid")
    map_path = _write_map(
        tmp_path / "roles.toml",
        f'format_version = 1\nunmapped = "deny"\nsocket_group = "{MY_GROUP}"\n',
    )
    run_root = tmp_path / "run"
    run_root.mkdir()
    access = AccessControl.arm(map_path, run_root)
    armed_gid = access.policy.socket_gid
    assert armed_gid == os.getgid()

    import dsl41.runner_access as ra

    renumbered = grp.struct_group((MY_GROUP, "", armed_gid + 1, []))
    monkeypatch.setattr(ra.grp, "getgrnam", lambda name: renumbered)
    access.reload()

    assert access.policy.generation == 1  # the old policy stands
    assert access.policy.socket_gid == armed_gid
    failures = [r for r in _receipts(run_root) if r["rec"] == "policy_reload_failed"]
    assert "name and gid alike" in failures[0]["error"]


def test_access_peer_principal_getsockopt_failure_is_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ss3: a getsockopt that raises resolves to None -- the caller's
    refusal path, never an unexplained traceback."""
    import dsl41.runner_access as ra

    def broken(sock: object) -> int:
        raise OSError("credential unavailable")

    monkeypatch.setattr(ra, "peer_uid", broken)
    left, right = _same_uid_socketpair()
    try:
        assert ra.peer_principal(left) is None
    finally:
        left.close()
        right.close()


def test_access_map_must_be_a_regular_file(tmp_path: Path) -> None:
    """ss4: a directory refuses; a FIFO refuses WITHOUT blocking (the
    open is non-blocking, so a reload cannot be parked on a pipe)."""
    as_dir = tmp_path / "mapdir"
    as_dir.mkdir()
    with pytest.raises(AccessError, match="not a regular file"):
        load_policy(as_dir, generation=1)
    fifo = tmp_path / "map.fifo"
    os.mkfifo(fifo)
    with pytest.raises(AccessError, match="not a regular file"):
        load_policy(fifo, generation=1)


def test_access_map_short_reads_assemble_the_whole_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss7/DL-149: the loader reads to EOF. A read that returns one byte
    at a time still yields the complete map — the digest proves the
    exact bytes, so no valid-TOML prefix can install."""
    map_path = _map_granting(tmp_path / "roles.toml", "ops")
    raw = map_path.read_bytes()
    real_read = os.read

    def dribble(fd: int, n: int) -> bytes:
        return real_read(fd, 1)

    monkeypatch.setattr(os, "read", dribble)
    policy = load_policy(map_path, generation=1)
    monkeypatch.setattr(os, "read", real_read)
    assert policy.resolve(Principal("os", ME, frozenset())) is Tier.OPS
    assert policy.digest == "sha256:" + hashlib.sha256(raw).hexdigest()


def test_access_map_descriptor_io_failure_is_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss4/DL-149: an OSError in the loader's descriptor I/O surfaces
    as AccessError, the same refusal every other load defect raises."""
    map_path = _map_granting(tmp_path / "roles.toml", "ops")
    real_read = os.read

    def broken(fd: int, n: int) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "read", broken)
    try:
        with pytest.raises(AccessError, match="descriptor I/O failed"):
            load_policy(map_path, generation=1)
    finally:
        monkeypatch.setattr(os, "read", real_read)


def test_access_map_over_the_ceiling_refuses(tmp_path: Path) -> None:
    """ss4/DL-149: the loader refuses past 1 MiB instead of reading a
    growing or mistaken file whole on the event loop."""
    body = 'format_version = 1\nunmapped = "deny"\n' + "# padding\n" * (1 << 17)
    map_path = _write_map(tmp_path / "roles.toml", body)
    with pytest.raises(AccessError, match="over the 1 MiB ceiling"):
        load_policy(map_path, generation=1)


def test_access_map_parser_blowup_is_a_refusal(tmp_path: Path) -> None:
    """ss7/DL-149: tomllib raises OUTSIDE TOMLDecodeError too — a
    several-thousand-digit integer literal raises BARE ValueError (the
    CPython digit limit) — and the loader must answer AccessError, not
    let a raw parser error escape reload."""
    map_path = _write_map(tmp_path / "roles.toml", "format_version = " + "1" * 5000 + "\n")
    with pytest.raises(AccessError, match="not valid TOML"):
        load_policy(map_path, generation=1)


def test_access_reload_receipts_a_mid_read_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss7/DL-149: reload over a descriptor that fails mid-read does not
    raise — it writes policy_reload_failed and keeps the old snapshot."""
    map_path = _map_granting(tmp_path / "roles.toml", "ops")
    run_root = tmp_path / "run"
    run_root.mkdir()
    access = AccessControl.arm(map_path, run_root)
    real_read = os.read

    def broken(fd: int, n: int) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "read", broken)
    try:
        access.reload()
    finally:
        monkeypatch.setattr(os, "read", real_read)
    assert access.policy.generation == 1  # the old snapshot stayed active
    recs = _receipts(run_root)
    assert [r["rec"] for r in recs] == ["policy_loaded", "policy_reload_failed"]
    assert recs[1]["generation"] == 1
    assert "descriptor I/O failed" in recs[1]["error"]


def test_access_unreadable_existing_journal_refuses_arming(tmp_path: Path) -> None:
    """ss6: an existing journal this process cannot read must not be
    silently restarted at seq 1 -- a repeated key is a forged identity."""
    from dsl41.runner_access import PerimeterJournal

    path = tmp_path / "perimeter.jsonl"
    PerimeterJournal(path).write("policy_loaded", sync=True, generation=1)
    os.chmod(path, 0o200)  # append-able, unreadable
    try:
        with pytest.raises(AccessError, match="cannot recover seq"):
            PerimeterJournal(path)
    finally:
        os.chmod(path, 0o600)


def test_access_torn_mid_line_write_never_glues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss6: a write that fails MID-LINE leaves a torn fragment; the next
    write starts on a fresh line instead of gluing to it."""
    import dsl41.runner_access as ra
    from dsl41.runner_access import PerimeterJournal

    journal = PerimeterJournal(tmp_path / "p.jsonl")
    real_write = os.write
    calls = {"n": 0}

    def partial_then_die(fd: int, view) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, bytes(view[:7]))  # seven bytes land ...
        raise OSError("disk vanished")  # ... then the line tears

    monkeypatch.setattr(ra.os, "write", partial_then_die)
    assert journal.write("access_denied", sync=False, principal="x") is False
    # a ZERO-byte write must fail the gate too, not loop forever
    monkeypatch.setattr(ra.os, "write", lambda fd, view: 0)
    assert journal.write("access_denied", sync=False, principal="x") is False
    monkeypatch.setattr(ra.os, "write", real_write)
    assert journal.write("policy_loaded", sync=True, generation=1) is True
    lines = (tmp_path / "p.jsonl").read_bytes().splitlines()
    assert len(lines) == 2  # the fragment, then a WHOLE record after it
    json.loads(lines[1])  # parses -- it never glued to the fragment
    with pytest.raises(json.JSONDecodeError):
        json.loads(lines[0])  # the fragment stays what it is


def test_access_cli_run_refuses_an_unknown_socket_group_before_the_root(
    short_root: Path,
) -> None:
    """ss4/ss8 preflight: a socket_group this host cannot resolve refuses
    BEFORE the root is claimed -- no estate residue."""
    estate = short_root / "estate.jil"
    estate.write_text(TEXT)
    map_path = _write_map(
        short_root / "roles.toml",
        'format_version = 1\nsocket_group = "dsl41-no-such-group-zz"\n',
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "dsl41",
            "run",
            str(estate),
            "--run-root",
            str(short_root / "run"),
            "--access-map",
            str(map_path),
            "--as-machine",
            "m1",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "socket_group" in proc.stderr
    assert not (short_root / "run").exists()
    assert not (short_root / "run.anchor").exists()


# ------------------------- ss12 obligations 5-7 at the CALLER, and two arms


class _PickyJournal:
    """A perimeter journal whose named record kinds never land.

    ss12 obligations 5-7 each say a receipt is best effort: the denial, the
    admission and the revocation stand on their own. The shipped journal
    answers False only on a storage failure, so a test of the CALLER needs
    the failure injected here (DL-151)."""

    def __init__(self, path: Path, fails: frozenset[str]) -> None:
        self.path = path
        self.fails = fails
        self.calls: list[tuple[str, bool]] = []

    def write(self, rec: str, *, sync: bool, **fields: object) -> bool:
        self.calls.append((rec, sync))
        return rec not in self.fails


def test_access_a_denial_stands_when_its_receipt_cannot_be_written(tmp_path: Path) -> None:
    """ss6/ss12.5: the receipt does not gate the decision. A denial whose
    access_denied write fails is still a denial, and it is attempted
    SYNCED."""
    map_path = _map_granting(tmp_path / "roles.toml", "read")
    run_root = tmp_path / "run"
    run_root.mkdir()
    access = AccessControl.arm(map_path, run_root)
    journal = _PickyJournal(run_root / "perimeter.jsonl", frozenset({"access_denied"}))
    access.journal = journal  # type: ignore[assignment]
    allowed, why = access.decide(Principal("os", ME, frozenset()), "sendevent", "STARTJOB")
    assert allowed is False
    assert "needs ops tier" in why
    assert journal.calls == [("access_denied", True)]


def test_access_an_admission_stands_when_its_ledger_line_cannot_be_written(
    tmp_path: Path,
) -> None:
    """ss6/ss12.6: the privileged ledger is corroborating and best effort --
    a full disk must not become a total operations outage."""
    map_path = _map_granting(tmp_path / "roles.toml", "ops")
    run_root = tmp_path / "run"
    run_root.mkdir()
    access = AccessControl.arm(map_path, run_root)
    journal = _PickyJournal(run_root / "perimeter.jsonl", frozenset({"privileged_admitted"}))
    access.journal = journal  # type: ignore[assignment]
    allowed, why = access.decide(Principal("os", ME, frozenset()), "sendevent", "STARTJOB")
    assert (allowed, why) == (True, "")
    assert journal.calls == [("privileged_admitted", False)]  # attempted, unsynced


def test_access_a_revocation_stands_when_its_receipt_cannot_be_written(tmp_path: Path) -> None:
    """ss7/ss12.7: revocation is mandatory and its receipt best effort. The
    stream that lost read closes and is dropped even when stream_revoked
    never lands -- while policy_loaded stays the gate it is."""
    map_path = _map_granting(tmp_path / "roles.toml", "ops")
    run_root = tmp_path / "run"
    run_root.mkdir()
    access = AccessControl.arm(map_path, run_root)
    journal = _PickyJournal(run_root / "perimeter.jsonl", frozenset({"stream_revoked"}))
    access.journal = journal  # type: ignore[assignment]

    class _Writer:
        closed = False

        def close(self) -> None:
            type(self).closed = True

    class _Task:
        cancelled = False

        def cancel(self) -> None:
            type(self).cancelled = True

    writer, task = _Writer(), _Task()
    access.streams = {writer: (Principal("os", "someone-else", frozenset()), task)}
    _map_granting(tmp_path / "roles.toml", "ops")  # the loser is unmapped either way
    access.reload()
    assert access.policy.generation == 2  # the install itself happened
    assert access.streams == {}
    assert _Writer.closed is True and _Task.cancelled is True
    assert journal.calls == [("policy_loaded", True), ("stream_revoked", True)]


def test_access_the_denial_receipt_is_on_disk_before_the_answer_arrives(
    short_root: Path,
) -> None:
    """ss6/ss12.5 at the real door: `access_denied` is synced BEFORE the
    refusal is answered, so a client holding the refusal is holding proof
    the receipt is already durable."""

    async def scenario() -> None:
        run_root = short_root / "run"
        map_path = _map_granting(short_root / "roles.toml", "read")
        engine, server, loop_task, _access = await _serve_armed(run_root, TEXT, map_path)
        try:
            status = await _call(server.path, read_for("job:acc_job"))
            denied = await _call(server.path, _envelope("STARTJOB", "acc_job", status))
            assert denied["refused"] is True
            # read the journal with the engine still up and the answer in hand
            denials = [r for r in _receipts(run_root) if r["rec"] == "access_denied"]
            assert [r["action"] for r in denials] == ["sendevent:STARTJOB"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_access_map_parser_recursion_is_a_refusal(tmp_path: Path) -> None:
    """ss4/ss12.11: RecursionError is the second source named in the
    loader's parse arm (DL-149) and nothing reached it -- deeply nested
    TOML raises it, and the loader must answer AccessError like every other
    parse failure, so reload receipts it instead of raising."""
    body = "format_version = " + "[" * 20000 + "]" * 20000 + "\n"
    map_path = _write_map(tmp_path / "roles.toml", body)
    with pytest.raises(AccessError, match="not valid TOML"):
        load_policy(map_path, generation=1)


def test_access_peer_principal_refuses_when_the_group_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ss3/ss12.11: os.getgrouplist is the second source named in that
    refusal arm. An OSError there is NO resolvable credential -- not a
    principal with an empty group set, which would still match user rows."""
    import dsl41.runner_access as ra

    def nss_said_no(name: str, gid: int) -> list[int]:
        raise OSError("group lookup failed")

    monkeypatch.setattr(ra.os, "getgrouplist", nss_said_no)
    left, right = _same_uid_socketpair()
    try:
        assert ra.peer_principal(left) is None
    finally:
        left.close()
        right.close()


def test_access_seq_recovery_reads_records_only(tmp_path: Path) -> None:
    """ss6: recovery reads the last complete RECORD. A readable file with
    none starts the series at zero -- a valid-JSON line that is not an
    object used to raise AttributeError out of arming, and `true` is not a
    sequence number."""
    from dsl41.runner_access import PerimeterJournal

    path = tmp_path / "perimeter.jsonl"
    path.write_text('[]\n"a string"\n{"rec": "x", "access_seq": true}\n{"rec": "y"}\n')
    journal = PerimeterJournal(path)
    assert journal.write("policy_loaded", sync=True, generation=1) is True
    records = [json.loads(line) for line in path.read_text().splitlines()[4:]]
    assert [r["access_seq"] for r in records] == [1]


def test_access_the_created_journal_is_durable_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss6: a create is a directory-entry write. Without the parent fsync
    the NAME can vanish on power loss after arm() accepted the receipt as
    synced, and access_seq would restart and forge duplicate audit keys."""
    import dsl41.runner_access as ra
    from dsl41.runner_access import PerimeterJournal

    run_root = tmp_path / "run"
    run_root.mkdir()
    synced: list[str] = []
    real_fsync_dir = ra.fsync_dir

    def record(path: object) -> None:
        synced.append(str(path))
        real_fsync_dir(path)

    monkeypatch.setattr(ra, "fsync_dir", record)
    journal = PerimeterJournal(run_root / "perimeter.jsonl")
    assert journal.write("policy_loaded", sync=True, generation=1) is True
    assert synced == [str(run_root)]  # the create, and only the create
    assert journal.write("access_denied", sync=True, principal="x") is True
    assert synced == [str(run_root)]

    # a directory fsync that fails is a storage failure like any other
    def refuse(path: object) -> None:
        raise OSError("no fsync on this directory")

    monkeypatch.setattr(ra, "fsync_dir", refuse)
    second = PerimeterJournal(tmp_path / "other" / "perimeter.jsonl")
    (tmp_path / "other").mkdir()
    assert second.write("policy_loaded", sync=True, generation=1) is False
