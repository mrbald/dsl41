"""Passively record source-lane size calls, then run its Python entrypoint."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import sys
from functools import wraps
from pathlib import Path
from typing import Any


def install_recorder(observations: Path) -> None:
    def record(value: dict[str, Any]) -> None:
        with observations.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")

    record(
        {
            "kind": "wrapper-start",
            "COLUMNS": os.environ.get("COLUMNS"),
            "LINES": os.environ.get("LINES"),
            "TERM": os.environ.get("TERM"),
            "python_executable": sys.executable,
            "python_version": sys.version,
            "environment_keys": sorted(os.environ),
        }
    )
    original = shutil.get_terminal_size
    sequence = 0

    @wraps(original)
    def observed_size(*args: Any, **kwargs: Any) -> os.terminal_size:
        nonlocal sequence
        sequence += 1
        number = sequence
        try:
            size = original(*args, **kwargs)
        except BaseException as exc:
            record({"kind": "call", "sequence": number, "exception_type": type(exc).__name__})
            raise
        record({"kind": "call", "sequence": number, "result": list(size)})
        return size

    shutil.get_terminal_size = observed_size


def main() -> None:
    install_recorder(Path(sys.argv[1]))
    command = sys.argv[2:]
    if command[0] == "-m":
        sys.argv = command[1:]
        runpy.run_module(command[1], run_name="__main__", alter_sys=True)
    else:
        sys.argv = command
        runpy.run_path(command[0], run_name="__main__")


if __name__ == "__main__":
    main()
