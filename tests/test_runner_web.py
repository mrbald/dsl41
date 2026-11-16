"""The pinned library application and actual person-aware UI children on Unix sockets.

DL-499 and DL-507 own the rules these tests pin.
"""

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

from dsl41.runner_web import _public_origin, _RequestHeaders, serve_people
from test_access import TEXT, _receipts, _serve_armed, _teardown
from test_control_as import policy

ORIGIN = "https://control.example.invalid"
BASE = ORIGIN + "/dsl41"
USER = "X-Example-User"


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@asynccontextmanager
async def running(root, control_socket, public_url=ORIGIN):
    path, log, output = root / "w", root / "events", root / "output"
    with output.open("wb") as stream:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).with_name("serve_people_support.py")),
            str(path),
            str(control_socket),
            str(log),
            public_url,
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
                            assert "wss" + public_url.removeprefix("https") + "/ws" in text
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
    ("public_url", "headers"),
    [
        *(
            (ORIGIN, headers)
            for headers in (
                [("Origin", ORIGIN)],
                [("Origin", ORIGIN), (USER, "alice"), (USER, "bob")],
                # DL-507: a repeated first header must not fall through to the second.
                [("Origin", ORIGIN), (USER, "alice"), (USER, "bob"), ("X-Example-Mail", "alice")],
                [("Origin", ORIGIN), (USER, "")],
                [("Origin", ORIGIN), (USER, " ")],
                *(
                    [("Origin", ORIGIN), (USER, user)]
                    for user in ("raise", "none", "bad", "surrogate")
                ),
                [("Origin", "https://other.example.invalid"), (USER, "alice")],
                [(USER, "alice")],
                [("Origin", "null"), (USER, "alice")],
            )
        ),
        (BASE, [("Origin", BASE), (USER, "alice")]),
    ],
)
def test_web_refuses_before_prepare_or_process(short_root, public_url, headers):
    async def scenario():
        async with running(short_root, short_root / "missing", public_url) as (client, log, _):
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
                    headers={"Origin": ORIGIN, USER: "alice"},
                ) as ws:
                    async with asyncio.timeout(12):
                        while not access.streams:
                            message = await ws.receive(timeout=12)
                            assert message.type == aiohttp.WSMsgType.BINARY, output.read_text()
                    assert {p.spelling for p, _ in access.streams.values()} == {"app/alice"}
                async with client.ws_connect(
                    "http://localhost/ws", headers={"Origin": ORIGIN, USER: "no-roles"}
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
                            "http://localhost/ws", headers={"Origin": ORIGIN, USER: name}
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


async def one_tab(short_root, headers, public_url=ORIGIN):
    """Open one tab on an armed engine; return the index page, principals and launched persons."""
    root = short_root / "r"
    engine, server, loop, access = await _serve_armed(root, TEXT, policy(short_root / "p"))
    try:
        async with running(short_root, server.path, public_url) as (client, log, output):
            async with client.get("http://localhost/") as index:
                page = await index.text()
            async with client.ws_connect(
                "http://localhost/ws?width=120&height=40", headers=headers
            ) as ws:
                async with asyncio.timeout(12):
                    while not access.streams:
                        message = await ws.receive(timeout=12)
                        assert message.type == aiohttp.WSMsgType.BINARY, output.read_text()
                principals = [principal for principal, _ in access.streams.values()]
            persons = [
                json.loads(shlex.split(record["command"])[8])
                for record in records(log)
                if record["kind"] == "launch"
            ]
    finally:
        await _teardown(engine, server, loop)
    return page, principals, persons


def test_web_lookup_falls_back_to_a_second_header(short_root):
    _, principals, persons = asyncio.run(
        one_tab(short_root, {"Origin": ORIGIN, "X-Example-Mail": "alice"})
    )
    assert [p.spelling for p in principals] == ["app/alice"]
    assert persons == [{"user": "alice", "roles": ["operators"]}]


def test_web_header_names_match_in_any_case(short_root):
    _, principals, persons = asyncio.run(
        one_tab(short_root, {"origin": ORIGIN, "x-example-user": "alice"})
    )
    assert [p.spelling for p in principals] == ["app/alice"]
    assert persons == [{"user": "alice", "roles": ["operators"]}]


def test_web_roles_header_reaches_engine_sorted(short_root):
    _, principals, persons = asyncio.run(
        one_tab(
            short_root, {"Origin": ORIGIN, USER: "alice", "X-Example-Roles": "operators;auditors"}
        )
    )
    assert [(p.spelling, p.groups) for p in principals] == [
        ("app/alice", frozenset({"auditors", "operators"}))
    ]
    assert persons == [{"user": "alice", "roles": ["auditors", "operators"]}]


def test_web_base_path_builds_urls_and_compares_origin_only(short_root):
    page, principals, persons = asyncio.run(
        one_tab(short_root, {"Origin": ORIGIN, USER: "alice"}, public_url=BASE)
    )
    assert "wss://control.example.invalid/dsl41/ws" in page
    assert "https://control.example.invalid/dsl41/static/" in page
    assert "https://control.example.invalid/static/" not in page
    assert [p.spelling for p in principals] == ["app/alice"]
    assert persons == [{"user": "alice", "roles": ["operators"]}]


def test_web_headers_reading_rules():
    headers = _RequestHeaders(
        [("X-One", "a"), ("X-Twice", "b"), ("x-twice", "c"), ("X-Blank", " \t"), ("X-Empty", "")]
    )
    assert headers["x-one"] == headers["X-ONE"] == "a"
    assert headers.get("X-Missing") is None
    with pytest.raises(KeyError):
        headers["X-Missing"]
    for name in ("X-Twice", "X-Blank", "X-Empty"):
        with pytest.raises(ValueError) as refusal:
            headers.get(name, "fallback")
        assert not isinstance(refusal.value, KeyError)
        assert "\n" not in str(refusal.value)
    assert list(headers) == ["x-one", "x-twice", "x-blank", "x-empty"]
    assert len(headers) == 4
    assert "X-TWICE" in headers
    assert "X-Missing" not in headers
    assert 1 not in headers


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://h", "https://h"),
        ("https://h:8443", "https://h:8443"),
        ("https://h/dsl41", "https://h"),
        ("https://h/a/b-c_d.e~f", "https://h"),
    ],
)
def test_web_public_url_accepts_base_path(url, origin):
    assert _public_origin(url) == origin


@pytest.mark.parametrize(
    "url",
    [
        ORIGIN + "/",
        BASE + "/",
        ORIGIN + "//",
        ORIGIN + "/a//b",
        ORIGIN + "/.",
        ORIGIN + "/..",
        ORIGIN + "/a/../b",
        BASE + "%2F",
        ORIGIN + "/a b",
        ORIGIN + "?query",
        BASE + "?query",
        ORIGIN + "#fragment",
        BASE + "#fragment",
        "https://a:b@host",
        "https://a@host/dsl41",
        ORIGIN + "/a\\b",
        ORIGIN + "/caf\u00e9",
        "ftp://host",
        "https://",
        "//host",
        "https://host:bad",
        "https://host:65536",
        "https://host:",
        "https://host/\n",
    ],
)
def test_web_public_url_refuses_at_start(url, short_root):
    with pytest.raises(ValueError) as refusal:
        serve_people(
            lambda headers: None,
            socket_path=short_root / "w",
            public_url=url,
            control_socket=short_root / "c",
        )
    assert "\n" not in str(refusal.value)
    assert not (short_root / "w").exists()


def test_web_missing_extra_is_one_plain_line(short_root, monkeypatch):
    monkeypatch.setitem(sys.modules, "textual_serve.server", None)
    with pytest.raises(SystemExit) as missing:
        serve_people(
            lambda headers: None,
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
        lambda headers: None,
        socket_path=short_root / "w",
        public_url=ORIGIN,
        control_socket=short_root / "c",
    )
    assert dict(os.environ) == before
