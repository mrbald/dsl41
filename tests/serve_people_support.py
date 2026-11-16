"""Real serve_people process with an observed child launcher and synthetic lookup.

DL-499, DL-507 and DL-509 own the rules it exercises.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from textual_serve.app_service import AppService
from textual_serve.server import Server

from dsl41.runner_web import serve_people


def main() -> None:
    socket_path, control_socket, log_path, public_url, *extra_origins = sys.argv[1:]
    log = Path(log_path)
    shared = []
    initialize = Server.__init__
    launch = AppService._open_app_process

    def record(value):
        with log.open("a") as stream:
            stream.write(json.dumps(value) + "\n")

    def observe_server(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        shared.append(self)

    async def observe_launch(self, width, height):
        process = await launch(self, width, height)
        record({"kind": "launch", "command": self.command, "shared_command": shared[0].command})
        return process

    def resolve(headers):
        # DL-507: a site may fall back to a second header; a blank or repeated first one refuses.
        user = headers.get("X-Example-User") or headers["X-Example-Mail"]
        record({"kind": "lookup", "user": user, "thread": threading.get_ident()})
        if user == "raise":
            raise RuntimeError("synthetic lookup failure")
        if user == "none":
            return None
        if user == "bad":
            return "alice", "operators"
        if user == "surrogate":
            return "\ud800", []
        if user == "no-roles":
            return user, []
        roles = headers.get("X-Example-Roles")
        return user, (["operators"] if roles is None else roles.split(";"))

    Server.__init__ = observe_server
    AppService._open_app_process = observe_launch
    record({"kind": "main", "thread": threading.get_ident()})
    serve_people(
        resolve,
        socket_path=socket_path,
        public_url=public_url,
        control_socket=control_socket,
        extra_origins=extra_origins,
    )


if __name__ == "__main__":
    main()
