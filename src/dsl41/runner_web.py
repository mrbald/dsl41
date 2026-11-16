"""Person-aware browser sessions behind an authenticating proxy (DL-499, DL-507).

The rules are in access-model ss9.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import shlex
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from dsl41.runner_access import validate_person

# DL-507: one path segment of `public_url`.
_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")


class _RequestHeaders(Mapping[str, str]):
    """Read-only snapshot of a request's headers, read by name in any case (DL-507).

    A name that was not sent raises KeyError. A name sent more than once, or with a
    blank value, raises ValueError, which `get` does not catch.
    """

    def __init__(self, pairs: Iterable[tuple[str, str]]) -> None:
        values: dict[str, tuple[str, ...]] = {}
        for name, value in pairs:
            key = name.lower()
            values[key] = values.get(key, ()) + (value,)
        self._values = values

    def __getitem__(self, name: str) -> str:
        values = self._values[name.lower()]
        if len(values) != 1:
            raise ValueError(f"header {name} was sent {len(values)} times")
        if not values[0].strip():
            raise ValueError(f"header {name} is blank")
        return values[0]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.lower() in self._values

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def _public_origin(public_url: str) -> str:
    """Return the `scheme://host[:port]` part of a valid `public_url` (DL-499, DL-507)."""
    parts = urlsplit(public_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.netloc.endswith(":")
        or any(char.isspace() for char in public_url)
        or public_url != f"{parts.scheme}://{parts.netloc}{parts.path}"
        or not all(
            _SEGMENT.fullmatch(segment) and segment not in {".", ".."}
            for segment in parts.path.split("/")[1:]
        )
    ):
        raise ValueError(
            "public_url must be http(s)://host[:port] followed by zero or more /segment,"
            " each of A-Za-z0-9._~- and neither . nor .."
        )
    # DL-499: urlsplit checks a port's spelling only when the port is read.
    parts.port
    return f"{parts.scheme}://{parts.netloc}"


def serve_people(
    resolve: Callable[[Mapping[str, str]], tuple[str, list[str]] | None],
    *,
    socket_path: str | Path,
    public_url: str,
    control_socket: str | Path,
) -> None:
    """Serve the library's application on a Unix socket with one person per tab (DL-499)."""
    origin = _public_origin(public_url)
    try:
        from aiohttp import web
        from textual_serve.server import Server
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "serve_people needs the optional [ui] extra: pip install 'dsl41[ui]'"
        ) from exc

    class PeopleServer(Server):
        async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
            # DL-507: a browser's Origin carries no path; it must equal public_url's origin.
            if request.headers.get("Origin") != origin:
                raise web.HTTPForbidden(text="Origin must equal public_url's scheme, host and port")
            headers = _RequestHeaders(request.headers.items())
            try:
                answer = await asyncio.to_thread(resolve, headers)
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
