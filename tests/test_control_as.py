"""The one-request person at the real control perimeter (DL-499, DL-491)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys

import pytest
from typer.testing import CliRunner

from dsl41.cli import app as cli
from dsl41.runner_access import validate_person
from dsl41.runner_control import ControlClient, versioned
from dsl41.runner_journal import read_journal
from dsl41.runner_tui import RunnerApp, SpecScreen, TriggersScreen
from test_access import ME, TEXT, _envelope, _receipts, _serve_armed, _teardown, _write_map
from test_runner_tui import _serve, _wait_for_ui


def person(user="alice", roles=("operators",)):
    return validate_person({"user": user, "roles": list(roles)})


def policy(path, subject="group:app/operators", tier="ops", *, os_tier=None):
    rows = f'[[binding]]\nsubject={json.dumps(subject)}\ntier="{tier}"\n' if tier else ""
    if os_tier:
        rows += f'[[binding]]\nsubject="user:os/{ME}"\ntier="{os_tier}"\n'
    return _write_map(path, 'format_version=1\nunmapped="deny"\n' + rows)


def wrap(request, user="alice", roles=("operators",)):
    return versioned({"cmd": "as", "user": user, "roles": list(roles), "request": request})


async def exchange(reader, writer, request):
    writer.write(json.dumps(versioned(request)).encode() + b"\n")
    await writer.drain()
    return json.loads(await asyncio.wait_for(reader.readline(), 5))


async def close(writer):
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


@pytest.mark.parametrize("tier", ["read", "ops", "adm"])
@pytest.mark.parametrize("subject", ["group:app/operators", "user:app/alice"])
def test_access_as_matrix_catalog_and_actor(short_root, tier, subject):
    async def scenario():
        root = short_root / "r"
        engine, server, loop, _ = await _serve_armed(
            root, TEXT, policy(short_root / "p", subject, tier)
        )
        client = ControlClient(
            server.path, person(roles=() if subject.startswith("user:") else ("operators",))
        )
        try:
            status = await client.request({"cmd": "status"})
            assert status["ok"]
            await client.close()
            assert (await client.request({"cmd": "status"}))["ok"]
            catalog = await client.request({"cmd": "catalog"})
            if tier == "read":
                assert catalog["environment_view"] == "read"
                assert catalog["job_env_names"] == []
                assert "job_env" not in catalog["settings"]
                assert "path_environment" not in catalog
            else:
                assert catalog["environment_view"] == "ops"
                assert "job_env" in catalog["settings"]
                assert isinstance(catalog["path_environment"], dict)
            mutation = await client.request(_envelope("ON_HOLD", "acc_job", status))
            assert mutation["ok"] is (tier != "read"), mutation
            for cmd in ("reread", "update"):
                request = {"cmd": cmd}
                if cmd == "update":
                    request.update(baseline_id=status["baseline_id"], request_id="matrix-update")
                answer = await client.request(request)
                assert answer["code"] == (
                    "invalid_argument" if tier == "adm" else "access_denied"
                ), answer
            stop = await client.request({"cmd": "engine_stop"})
            assert stop["ok"] is (tier == "adm")
        finally:
            await client.close()
            await _teardown(engine, server, loop)
        decisions = [r for r in _receipts(root) if "principal" in r]
        assert decisions and all(
            r["realm"] == "app" and r["principal"] == "alice" for r in decisions
        )
        assert all(r["action"] != "as" for r in decisions)
        actors = {r["claimed_actor"] for r in read_journal(root) if "claimed_actor" in r}
        assert actors == (set() if tier == "read" else {"app/alice"})

    asyncio.run(scenario())


def test_access_as_retry_identity_and_next_plain_request(short_root):
    async def scenario():
        root = short_root / "r"
        path = policy(short_root / "p", os_tier="ops")
        with path.open("a") as stream:
            stream.write('[[binding]]\nsubject="user:app/alice"\ntier="ops"\n')
        engine, server, loop, _ = await _serve_armed(root, TEXT, path)
        reader, writer = await asyncio.open_unix_connection(str(server.path))
        try:
            status = await exchange(reader, writer, wrap({"cmd": "status"}))
            request = _envelope("ON_HOLD", "acc_job", status)
            first = await exchange(reader, writer, wrap(request))
            assert first["ok"]
            retry = await exchange(reader, writer, wrap(request, roles=("another-role",)))
            assert retry == first
            reused = await exchange(reader, writer, wrap(request, user="bob"))
            assert reused["code"] == "request_id_reused"
            denied = await exchange(reader, writer, wrap({"cmd": "status"}, "unmapped", ()))
            assert denied["code"] == "access_denied"
            assert "app/unmapped" in denied["error"]
            current = await exchange(reader, writer, {"cmd": "status"})
            plain = await exchange(reader, writer, _envelope("OFF_HOLD", "acc_job", current))
            assert plain["ok"]
        finally:
            await close(writer)
            await _teardown(engine, server, loop)
        records = _receipts(root)
        assert any(r.get("realm") == "app" and r.get("principal") == "unmapped" for r in records)
        assert records[-1]["realm"] == "os" and records[-1]["principal"] == ME
        actors = {r["claimed_actor"] for r in read_journal(root) if "claimed_actor" in r}
        assert actors == {"app/alice", f"os/{ME}"}

    asyncio.run(scenario())


BAD_PERSONS = [
    None,
    [],
    {},
    {"user": "a"},
    {"roles": []},
    {"user": "a", "roles": [], "extra": 1},
    *({"user": value, "roles": []} for value in (None, 1, "", " \t", "\ud800")),
    *({"user": "a", "roles": value} for value in (None, "ops", {}, [1], [""], [" "], ["\udfff"])),
]


@pytest.mark.parametrize("value", BAD_PERSONS)
def test_access_bad_web_principal_never_connects(value, short_root, cli_env, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid person reached the socket probe")

    monkeypatch.setattr("dsl41.cli_control.check_control_socket", forbidden)
    result = CliRunner().invoke(
        cli, ["ui", "-S", str(short_root / "absent"), "--web-principal", json.dumps(value)]
    )
    assert result.exit_code == 2
    assert "invalid web principal" in result.stderr


def test_access_bad_web_principal_json_and_hidden_option(short_root, cli_env):
    result = CliRunner().invoke(
        cli, ["ui", "-S", str(short_root / "absent"), "--web-principal", "{"]
    )
    assert result.exit_code == 2 and "invalid web principal" in result.stderr
    assert "web-principal" not in CliRunner().invoke(cli, ["ui", "--help"]).stdout
    assert "web-principal" not in CliRunner().invoke(cli, ["run", "--help"]).stdout


def test_access_detached_cli_denied_status_exits_2(short_root, cli_env):
    async def scenario():
        root = short_root / "r"
        engine, server, loop, _ = await _serve_armed(
            root, TEXT, policy(short_root / "p", tier=None)
        )
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "dsl41",
                "ui",
                "-S",
                str(server.path),
                "--web-principal",
                json.dumps({"user": "alice", "roles": []}),
                env={
                    **os.environ,
                    "TEXTUAL_DRIVER": "textual.drivers.headless_driver:HeadlessDriver",
                },
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 2, stderr.decode()
            assert "app/alice holds no tier; status needs read tier" in stderr.decode()
            decisions = [r for r in _receipts(root) if "principal" in r]
            assert len(decisions) == 1 and decisions[0]["action"] == "status"
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            await _teardown(engine, server, loop)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"user": None},
        {"user": ""},
        {"user": " "},
        {"user": "\ud800"},
        {"roles": None},
        {"roles": "ops"},
        {"roles": [1]},
        {"roles": [" "]},
        {"roles": ["\ud800"]},
        {"request": None},
        {"request": []},
        {"request": {"cmd": "status", "v": 4}},
        {"request": {"cmd": "status", "v": True}},
        {"request": {"cmd": "status", "v": 5.0}},
        *({"omit": key} for key in ("user", "roles", "request")),
    ],
)
def test_access_as_malformed_is_headerless_unreceipted_and_keeps_connection(short_root, change):
    async def scenario():
        root = short_root / "r"
        engine, server, loop, _ = await _serve_armed(
            root, TEXT, policy(short_root / "p", os_tier="read")
        )
        reader, writer = await asyncio.open_unix_connection(str(server.path))
        try:
            before = _receipts(root)
            request = {**wrap({"cmd": "status"}), **change}
            if "omit" in request:
                request.pop(request.pop("omit"))
            answer = await exchange(reader, writer, request)
            assert answer["code"] == "invalid_argument" and answer["refused"] is True
            assert "baseline_id" not in answer and "applied_index" not in answer
            assert _receipts(root) == before
            assert (await exchange(reader, writer, {"cmd": "status"}))["ok"]
        finally:
            await close(writer)
            await _teardown(engine, server, loop)

    asyncio.run(scenario())


@pytest.mark.parametrize("uid", [None, -1, "error"])
def test_access_as_other_uid_is_unreceipted(short_root, monkeypatch, uid):
    """Replace only peer_uid's kernel answer at the as door; accept and engine stay real."""

    def peer_uid(sock):
        if uid == "error":
            raise OSError("synthetic credential read failure")
        return uid

    monkeypatch.setattr("dsl41.runner_control.peer_uid", peer_uid)

    async def scenario():
        root = short_root / "r"
        engine, server, loop, _ = await _serve_armed(
            root, TEXT, policy(short_root / "p", os_tier="read")
        )
        reader, writer = await asyncio.open_unix_connection(str(server.path))
        try:
            before = _receipts(root)
            answer = await exchange(reader, writer, {**wrap(None), "extra": True})
            assert answer == {
                "ok": False,
                "refused": True,
                "code": "access_denied",
                "error": "only the engine's own account may use as (access-model ss9)",
            }
            assert _receipts(root) == before
            assert (await exchange(reader, writer, {"cmd": "status"}))["ok"]
        finally:
            await close(writer)
            await _teardown(engine, server, loop)

    asyncio.run(scenario())


def test_access_as_version_precedes_wrapper_and_unarmed_refusal(short_root):
    async def scenario():
        engine, server, loop = await _serve(short_root / "r", TEXT)
        reader, writer = await asyncio.open_unix_connection(str(server.path))
        try:
            writer.write(b"[]\n")
            await writer.drain()
            malformed = json.loads(await asyncio.wait_for(reader.readline(), 2))
            assert malformed["code"] == "malformed_request"
            for v in (None, 4):
                answer = await exchange(reader, writer, {"v": v, "cmd": "as"})
                assert answer["code"] == "unsupported_version"
            answer = await exchange(reader, writer, {"cmd": "as"})
            assert answer == {
                "ok": False,
                "refused": True,
                "code": "access_denied",
                "error": "as needs an access file (access-model ss9)",
            }
            assert (await exchange(reader, writer, {"cmd": "status"}))["ok"]
            assert not _receipts(short_root / "r")
        finally:
            await close(writer)
            await _teardown(engine, server, loop)

    asyncio.run(scenario())


def test_access_as_nested_and_subscription_reload(short_root):
    async def scenario():
        root = short_root / "r"
        path = policy(short_root / "p", tier="read")
        engine, server, loop, access = await _serve_armed(root, TEXT, path)
        reader, writer = await asyncio.open_unix_connection(str(server.path))
        try:
            nested = await exchange(reader, writer, wrap(wrap({"cmd": "status"})))
            assert nested["code"] == "access_denied" and "as" in nested["error"]
            ack = await exchange(reader, writer, wrap({"cmd": "subscribe"}))
            assert ack["subscribed"]
            assert [p.spelling for p, _ in access.streams.values()] == ["app/alice"]
            policy(path, tier=None)
            access.reload()
            assert await asyncio.wait_for(reader.read(), 3) == b""
            revoked = [r for r in _receipts(root) if r["rec"] == "stream_revoked"]
            assert len(revoked) == 1 and revoked[0]["principal"] == "alice"
            assert revoked[0]["realm"] == "app"
        finally:
            await close(writer)
            await _teardown(engine, server, loop)

    asyncio.run(scenario())


def test_access_ui_wraps_every_path_and_echoes_person(short_root):
    """Real status, trace, explain, console mutation and subscription; OS account has no tier."""

    async def scenario():
        root = short_root / "r"
        engine, server, loop, access = await _serve_armed(root, TEXT, policy(short_root / "p"))
        viewer = RunnerApp(server.path, person=person())
        try:
            async with viewer.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(
                    pilot, lambda: viewer._selected == "acc_job" and bool(access.streams)
                )
                await viewer._refresh()
                await viewer._update_explain()
                await pilot.press(":")
                await pilot.press(*list("ON_HOLD acc_job"), "enter")
                await _wait_for_ui(pilot, lambda: viewer._jobs_snapshot["acc_job"]["on_hold"])
                assert viewer._claimed_actor == "app/alice"
                assert {p.spelling for p, _ in access.streams.values()} == {"app/alice"}
                records = _receipts(root)
                assert any(r.get("action") == "sendevent:ON_HOLD" for r in records)
                assert not any(r.get("realm") == "os" for r in records)
                assert not any(r["rec"] == "access_denied" for r in records)
                viewer.query_one("#jobs").focus()
                await pilot.press("t")
                await _wait_for_ui(pilot, lambda: isinstance(viewer.screen, TriggersScreen))
                await viewer._refresh()
                await pilot.press("escape")
        finally:
            await _teardown(engine, server, loop)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "owns_run,code,ends",
    [
        (False, "access_denied", True),
        (True, "access_denied", False),
        (False, "invalid_argument", False),
    ],
)
def test_ui_status_denial_exit_precedes_trace(short_root, owns_run, code, ends, capsys):
    """The socket stands in for an engine's fault response, without changing UI state."""

    async def scenario():
        requests = []

        async def handler(reader, writer):
            try:
                while line := await reader.readline():
                    request = json.loads(line)
                    requests.append(request["cmd"])
                    answer = {"ok": False, "code": code, "error": "synthetic status fault"}
                    writer.write(json.dumps(answer).encode() + b"\n")
                    await writer.drain()
                    if request["cmd"] == "subscribe":
                        break
            finally:
                await close(writer)

        path = short_root / "s"
        server = await asyncio.start_unix_server(handler, path=str(path))
        viewer = RunnerApp(path, owns_run=owns_run)
        try:
            async with viewer.run_test(size=(100, 35)) as pilot:
                await _wait_for_ui(pilot, lambda: "status" in requests)
                if not ends:
                    await _wait_for_ui(pilot, lambda: "trace" in requests)
                await pilot.pause()
            assert (viewer.return_code == 2) is ends
            assert ("trace" not in requests) is ends
        finally:
            server.close()
            await server.wait_closed()
        if ends:
            assert "synthetic status fault" in capsys.readouterr().err

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "operation,fault_cmd,code,owns_run,ends",
    [
        (operation, "status", code, owns_run, ends)
        for operation in ("details", "precondition")
        for code, owns_run, ends in (
            ("access_denied", False, True),
            ("access_denied", True, False),
            ("invalid_argument", False, False),
        )
    ]
    + [("details", cmd, "access_denied", False, False) for cmd in ("spec", "deps", "timers")]
    + [
        ("precondition", "sendevent", "access_denied", False, False),
        ("global", "global", "access_denied", False, False),
        ("details", "spec", "eof", False, False),
    ],
)
def test_ui_status_consumers_stop_only_on_detached_status_denial(
    short_root, operation, fault_cmd, code, owns_run, ends, capsys
):
    """The Unix socket replaces engine replies, including a per-job status denial (DL-499)."""

    async def scenario():
        requests, popups = [], []
        active = False
        failed = asyncio.Event()

        async def handler(reader, writer):
            try:
                while line := await reader.readline():
                    request = json.loads(line)
                    cmd = request["cmd"]
                    if active and (
                        cmd not in {"status", "trace", "explain", "subscribe"}
                        or (cmd == "status" and request.get("job") in {"visible", "unseen"})
                    ):
                        requests.append(cmd)
                    fault = active and cmd == fault_cmd
                    if cmd == "status":
                        fault = fault and request.get("job") == (
                            "visible" if operation == "details" else "unseen"
                        )
                    if fault:
                        failed.set()
                        if code == "eof":
                            return
                        answer = {"ok": False, "code": code, "error": "synthetic operation fault"}
                    else:
                        answer = {"ok": True, "baseline_id": "synthetic", "applied_index": 0}
                        if cmd == "status":
                            answer["jobs"] = {
                                request.get("job", "visible"): {
                                    "state_rev": 1,
                                    "status": "INACTIVE",
                                    "log_out": str(short_root / "missing-log"),
                                }
                            }
                        elif cmd == "deps":
                            answer.update(box_name="outer", members=["child"])
                        elif cmd == "trace":
                            answer.update(entries=[], last_seq=0)
                        elif cmd == "sendevent":
                            answer.update(decision="applied", kind=request["verb"])
                        elif cmd == "subscribe":
                            answer.update(subscribed=True, since=0)
                    writer.write(json.dumps(answer).encode() + b"\n")
                    await writer.drain()
                    if cmd == "subscribe":
                        await reader.read()
                        return
            finally:
                await close(writer)

        path = short_root / "s"
        server = await asyncio.start_unix_server(handler, path=str(path))
        viewer = RunnerApp(path, owns_run=owns_run)
        push = viewer.push_screen

        def observe(screen, *args, **kwargs):
            if isinstance(screen, SpecScreen):
                popups.append(screen)
            return push(screen, *args, **kwargs)

        viewer.push_screen = observe
        try:
            async with viewer.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: viewer._selected == "visible")
                await pilot.pause()
                active = True
                if operation == "details":
                    await pilot.press("d")
                else:
                    await pilot.press(":")
                    command = "SET_GLOBAL FLAG=value" if operation == "global" else "ON_HOLD unseen"
                    await pilot.press(*list(command), "enter")
                await asyncio.wait_for(failed.wait(), 3)
                if not ends:
                    if code == "eof":
                        await _wait_for_ui(pilot, lambda: viewer._connected is False)
                    elif operation == "details":
                        await _wait_for_ui(pilot, lambda: bool(popups))
                    else:
                        await _wait_for_ui(pilot, lambda: "sendevent" in requests)
                await pilot.pause()
            assert (viewer.return_code == 2) is ends
            if ends:
                assert requests == (["spec", "status"] if operation == "details" else ["status"])
                assert not popups
                assert "synthetic operation fault" in capsys.readouterr().err
            elif operation == "details" and code != "eof":
                assert requests[:4] == ["spec", "status", "deps", "timers"]
                assert len(popups) == 1
            elif operation != "details":
                assert requests[:2] == [
                    "global" if operation == "global" else "status",
                    "sendevent",
                ]
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_access_as_real_reread_and_update(short_root, cli_env):
    """A real CLI launch and worker keep strict reload envelopes behind the wrapper."""
    from test_main_config import config_file

    path = policy(short_root / "access.toml", tier="adm")
    jil = short_root / "estate.jil"
    local_text = TEXT.replace("machine: m1", "machine: localhost")
    jil.write_text(local_text)
    config = config_file(
        short_root, engine=f'access_map = "{path}"\nmachine_policy="local-eligible"\n'
    )

    async def scenario():
        output = short_root / "output"
        with output.open("wb") as stream:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "dsl41",
                "run",
                "--config",
                str(config),
                stdout=stream,
                stderr=stream,
            )
        client = ControlClient(short_root / "run/control.sock", person())
        try:
            async with asyncio.timeout(12):
                while not client.path.exists():
                    assert process.returncode is None, output.read_text()
                    await asyncio.sleep(0.02)
            status = await client.request({"cmd": "status"})
            reread = await client.request({"cmd": "reread"})
            assert reread["ok"], reread
            jil.write_text(local_text + "description: synthetic update\n")
            updated = await client.request(
                {
                    "cmd": "update",
                    "baseline_id": status["baseline_id"],
                    "request_id": "person-update",
                    "wait": True,
                }
            )
            assert updated["ok"], updated
            assert updated["application"]["rev"] == 2
            assert (await client.request({"cmd": "engine_stop"}))["ok"]
            assert await asyncio.wait_for(process.wait(), 10) == 0, output.read_text()
            decisions = [r for r in _receipts(short_root / "run") if "principal" in r]
            assert all(r["realm"] == "app" and r["principal"] == "alice" for r in decisions)
            assert {r["action"] for r in decisions} >= {"reread", "update"}
        finally:
            await client.close()
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), 10)

    asyncio.run(scenario())


def test_access_ui_refresh_finishes_across_teardown(short_root):
    """Hold only a completed socket read across the real UI's unmount (DL-499)."""

    async def scenario():
        root = short_root / "r"
        engine, server, loop, _ = await _serve_armed(root, TEXT, policy(short_root / "p"))
        viewer = RunnerApp(server.path, person=person())
        received, release = asyncio.Event(), asyncio.Event()
        pending = None
        try:
            async with viewer.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: viewer._selected == "acc_job")
                request = viewer._client.request

                async def delayed(payload):
                    answer = await request(payload)
                    if payload["cmd"] == "status":
                        received.set()
                        await release.wait()
                    return answer

                viewer._client.request = delayed
                pending = asyncio.create_task(viewer._refresh())
                await asyncio.wait_for(received.wait(), 3)
            release.set()
            await asyncio.wait_for(pending, 3)
            assert not viewer._refreshing
        finally:
            release.set()
            if pending is not None:
                await pending
            await viewer._client.close()
            await _teardown(engine, server, loop)

    asyncio.run(scenario())
