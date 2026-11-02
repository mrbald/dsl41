"""A pipe-acknowledged version of the wrapper's test pause hook (DL-42)."""

import io
import json
import os
from pathlib import Path
import runpy
import sys


def main() -> int:
    text = sys.stdin.read()
    spec = json.loads(text)
    sys.stdin = io.StringIO(text)
    wrapper = runpy.run_path(str(Path(__file__).parents[1] / "src/dsl41/runner_wrapper.py"))
    run = wrapper["main"]
    environment = spec.get("env", {})
    if environment.get("BARRIER_NO_WAITID"):
        if hasattr(os, "waitid"):
            del os.waitid

    def pause(point: str) -> None:
        if point != environment.get("BARRIER_PAUSE"):
            return
        root = Path(environment["BARRIER_PIPES"])
        with (root / "ack").open("wb", buffering=0) as ack:
            ack.write(b"paused")
        with (root / "release").open("rb", buffering=0) as release:
            if release.read(1) != b"r":
                raise RuntimeError("barrier release pipe closed")

    run.__globals__["_test_pause"] = pause
    return run()


if __name__ == "__main__":
    sys.exit(main())
