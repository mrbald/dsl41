"""Real serve_people process with an observed child launcher and synthetic lookup (DL-499)."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from textual_serve.app_service import AppService
from textual_serve.server import Server

from dsl41.runner_web import serve_people


def main() -> None:
    socket_path, control_socket, log_path = sys.argv[1:]
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

    def resolve(email):
        record({"kind": "lookup", "email": email, "thread": threading.get_ident()})
        if email == "raise":
            raise RuntimeError("synthetic lookup failure")
        if email == "none":
            return None
        if email == "bad":
            return "alice", "operators"
        if email == "surrogate":
            return "\ud800", []
        if email == "no-roles":
            return email, []
        return email, ["operators"]

    Server.__init__ = observe_server
    AppService._open_app_process = observe_launch
    record({"kind": "main", "thread": threading.get_ident()})
    serve_people(
        resolve,
        socket_path=socket_path,
        public_url="https://control.example.invalid",
        control_socket=control_socket,
    )


if __name__ == "__main__":
    main()
