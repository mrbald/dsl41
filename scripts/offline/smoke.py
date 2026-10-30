#!/usr/bin/env python3
"""Exercise installed compiler code outside the source checkout (DL-389)."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import dsl41


def main() -> None:
    candidate, profile, bundle = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    version = json.loads((bundle / "manifest.json").read_text())["version"]
    assert candidate in Path(dsl41.__file__).resolve().parents, "dsl41 imported outside candidate"
    assert importlib.metadata.version("dsl41") == version, "wrong installed release"
    direct = importlib.metadata.distribution("dsl41").read_text("direct_url.json")
    assert not direct or not json.loads(direct).get("dir_info", {}).get("editable")
    if profile == "ui":
        import textual
        import textual_serve

        assert candidate in Path(textual.__file__).resolve().parents
        assert candidate in Path(textual_serve.__file__).resolve().parents
    else:
        assert importlib.util.find_spec("textual") is None, "base unexpectedly contains UI"
    with tempfile.TemporaryDirectory(prefix="dsl41-offline-smoke-") as directory:
        work = Path(directory)
        estate = work / "smoke.jil"
        estate.write_text(
            "insert_job: offline_smoke\njob_type: CMD\n"
            "command: /bin/echo offline-smoke\nmachine: localhost\n"
        )
        cli = str(candidate / "bin/dsl41")
        for args in (
            ["--help"],
            ["viz", "--format", "explore", str(estate), "-o", str(work / "graph.html")],
        ):
            subprocess.run([cli, *args], cwd=work, check=True, capture_output=True, timeout=60)
        page = (work / "graph.html").read_text()
        assert "<!doctype html" in page[:256].lower() and "cytoscape" in page
    print(f"ok: installed {version} {profile} import, CLI and explore assets")


if __name__ == "__main__":
    main()
