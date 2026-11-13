"""Person-aware browser sessions behind an authenticating proxy (DL-499, access-model ss9)."""

from __future__ import annotations

import asyncio
import copy
import json
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from dsl41.runner_access import validate_person


def serve_people(
    resolve: Callable[[str], tuple[str, list[str]] | None],
    *,
    socket_path: str | Path,
    public_url: str,
    control_socket: str | Path,
) -> None:
    """Serve the library's application on a Unix socket with one person per tab (DL-499)."""
    origin = urlsplit(public_url)
    if (
        origin.scheme not in {"http", "https"}
        or not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or origin.netloc.endswith(":")
        or any(char.isspace() for char in public_url)
        or public_url != f"{origin.scheme}://{origin.netloc}"
    ):
        raise ValueError("public_url must contain only scheme, host and optional port")
    # DL-499: urlsplit checks a port's spelling only when the port is read.
    origin.port
    try:
        from aiohttp import web
        from textual_serve.server import Server
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "serve_people needs the optional [ui] extra: pip install 'dsl41[ui]'"
        ) from exc

    class PeopleServer(Server):
        async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
            if request.headers.get("Origin") != public_url:
                raise web.HTTPForbidden(text="Origin must equal public_url")
            emails = request.headers.getall("X-DSL41-Email", [])
            if len(emails) != 1 or not emails[0].strip():
                raise web.HTTPForbidden(text="one non-empty X-DSL41-Email is required")
            try:
                answer = await asyncio.to_thread(resolve, emails[0])
                if answer is None:
                    raise ValueError("no person")
                user, roles = answer
                person = validate_person({"user": user, "roles": roles})
            except Exception as exc:
                raise web.HTTPForbidden(text="person lookup refused") from exc
            tab = copy.copy(self)
            identity = json.dumps({"user": person.name, "roles": sorted(person.groups)})
            tab.command = "exec " + shlex.join(
                [
                    sys.executable,
                    "-m",
                    "dsl41",
                    "ui",
                    "--socket",
                    str(control_socket),
                    "--web-principal",
                    identity,
                ]
            )
            return await super(PeopleServer, tab).handle_websocket(request)

    server = PeopleServer("", title="dsl41", public_url=public_url)
    web.run_app(server._make_app(), path=str(socket_path))
