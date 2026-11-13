"""The pinned library application and actual person-aware UI children on Unix sockets (DL-499)."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest

from dsl41.runner_web import serve_people
from test_access import TEXT, _receipts, _serve_armed, _teardown
from test_control_as import policy

ORIGIN = "https://control.example.invalid"


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@asynccontextmanager
async def running(root, control_socket):
    path, log, output = root / "w", root / "events", root / "output"
    with output.open("wb") as stream:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).with_name("serve_people_support.py")),
            str(path),
            str(control_socket),
            str(log),
            stdout=stream,
            stderr=stream,
        )
    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=str(path)), timeout=aiohttp.ClientTimeout(total=12)
        ) as client:
            async with asyncio.timeout(12):
                while True:
                    assert process.returncode is None, output.read_text()
                    try:
                        async with client.get("http://localhost/") as index:
                            assert index.status == 200
                            text = await index.text()
                            assert "wss://control.example.invalid/ws" in text
                            assert "dsl41" in text
                            break
                    except aiohttp.ClientConnectorError:
                        await asyncio.sleep(0.02)
            yield client, log, output
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            assert await asyncio.wait_for(process.wait(), 10) == 0, output.read_text()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.parametrize(
    "headers",
    [
        [("Origin", ORIGIN)],
        [("Origin", ORIGIN), ("X-DSL41-Email", "alice"), ("X-DSL41-Email", "bob")],
        [("Origin", ORIGIN), ("X-DSL41-Email", "")],
        [("Origin", ORIGIN), ("X-DSL41-Email", " ")],
        *(
            [("Origin", ORIGIN), ("X-DSL41-Email", email)]
            for email in ("raise", "none", "bad", "surrogate")
        ),
        [("Origin", "https://other.example.invalid"), ("X-DSL41-Email", "alice")],
        [("X-DSL41-Email", "alice")],
    ],
)
def test_web_refuses_before_prepare_or_process(short_root, headers):
    async def scenario():
        async with running(short_root, short_root / "missing") as (client, log, _):
            with pytest.raises(aiohttp.WSServerHandshakeError) as refusal:
                await client.ws_connect("http://localhost/ws", headers=headers)
            assert refusal.value.status == 403
            assert not [r for r in records(log) if r["kind"] == "launch"]

    asyncio.run(scenario())


def test_web_tab_reaches_engine_and_unmapped_person_ends(short_root):
    async def scenario():
        root = short_root / "r"
        engine, server, loop, access = await _serve_armed(root, TEXT, policy(short_root / "p"))
        try:
            async with running(short_root, server.path) as (client, log, output):
                async with client.ws_connect(
                    "http://localhost/ws?width=120&height=40",
                    headers={"Origin": ORIGIN, "X-DSL41-Email": "alice"},
                ) as ws:
                    async with asyncio.timeout(12):
                        while not access.streams:
                            message = await ws.receive(timeout=12)
                            assert message.type == aiohttp.WSMsgType.BINARY, output.read_text()
                    assert {p.spelling for p, _ in access.streams.values()} == {"app/alice"}
                async with client.ws_connect(
                    "http://localhost/ws", headers={"Origin": ORIGIN, "X-DSL41-Email": "no-roles"}
                ) as denied:
                    async with asyncio.timeout(12):
                        while (await denied.receive()).type not in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                        }:
                            pass
                denials = [r for r in _receipts(root) if r["rec"] == "access_denied"]
                assert len(denials) == 1 and denials[0]["principal"] == "no-roles"
                assert denials[0]["action"] == "status"
                assert denials[0]["realm"] == "app"
                assert not any(r.get("realm") == "os" for r in _receipts(root))
                events = records(log)
                main = next(r["thread"] for r in events if r["kind"] == "main")
                assert all(r["thread"] != main for r in events if r["kind"] == "lookup")
                assert all(r["shared_command"] == "" for r in events if r["kind"] == "launch")
        finally:
            await _teardown(engine, server, loop)

    asyncio.run(scenario())


def test_web_two_tabs_and_shell_characters_keep_their_persons(short_root):
    async def scenario():
        root = short_root / "r"
        engine, server, loop, access = await _serve_armed(root, TEXT, policy(short_root / "p"))
        # DL-499: the shell must pass this spelling literally, including blanks and quotes.
        literal = "a 'quoted' \"person\" $(touch " + str(short_root / "injected") + ")"
        try:
            async with running(short_root, server.path) as (client, log, _):
                tabs = await asyncio.gather(
                    *(
                        client.ws_connect(
                            "http://localhost/ws", headers={"Origin": ORIGIN, "X-DSL41-Email": name}
                        )
                        for name in ("alice", literal)
                    )
                )
                try:
                    async with asyncio.timeout(12):
                        while len(access.streams) != 2:
                            await asyncio.sleep(0.02)
                    assert {p.spelling for p, _ in access.streams.values()} == {
                        "app/alice",
                        "app/" + literal,
                    }
                    commands = [r for r in records(log) if r["kind"] == "launch"]
                    assert len(commands) == 2
                    for record in commands:
                        argv = shlex.split(record["command"])
                        assert argv[:2] == ["exec", sys.executable]
                        assert argv[2:7] == ["-m", "dsl41", "ui", "--socket", str(server.path)]
                        assert json.loads(argv[8])["user"] in {"alice", literal}
                        assert record["shared_command"] == ""
                    assert not (short_root / "injected").exists()
                finally:
                    await asyncio.gather(*(tab.close() for tab in tabs))
        finally:
            await _teardown(engine, server, loop)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        ORIGIN + "/",
        ORIGIN + "/path",
        ORIGIN + "?query",
        ORIGIN + "#fragment",
        "https://a:b@host",
        "https://",
        "//host",
        "https://host:bad",
        "https://host:65536",
        "https://host:",
        "https://host/\n",
    ],
)
def test_web_public_url_refuses_at_start(url, short_root):
    with pytest.raises(ValueError):
        serve_people(
            lambda email: None,
            socket_path=short_root / "w",
            public_url=url,
            control_socket=short_root / "c",
        )
    assert not (short_root / "w").exists()


def test_web_missing_extra_is_one_plain_line(short_root, monkeypatch):
    monkeypatch.setitem(sys.modules, "textual_serve.server", None)
    with pytest.raises(SystemExit) as missing:
        serve_people(
            lambda email: None,
            socket_path=short_root / "w",
            public_url=ORIGIN,
            control_socket=short_root / "c",
        )
    assert (
        str(missing.value) == "serve_people needs the optional [ui] extra: pip install 'dsl41[ui]'"
    )


def test_web_run_app_has_only_unix_path_and_own_signals(short_root, monkeypatch):
    """Observe run_app arguments; other tests execute the real run_app in a child."""
    from aiohttp import web

    def run_app(application, **kwargs):
        application.close()
        assert kwargs == {"path": str(short_root / "w")}

    monkeypatch.setattr(web, "run_app", run_app)
    before = dict(os.environ)
    serve_people(
        lambda email: None,
        socket_path=short_root / "w",
        public_url=ORIGIN,
        control_socket=short_root / "c",
    )
    assert dict(os.environ) == before
