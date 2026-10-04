#!/usr/bin/env python3
"""Branch-only coverage for the whole `src/dsl41` package (DL-265). A report, not a gate.

Run it after `coverage run -m pytest -q` and `coverage combine`:

    uv run python scripts/branch_coverage.py

It asks coverage for JSON over every module of the package, so the gate's
`[tool.coverage.report] include` list does not narrow it. It prints
`covered_branches` over `num_branches` per module, most missed branches
first, then the package total. The `Cover` column of `coverage report` mixes
statements and branches; these numbers are branches only. It always exits 0
once the data is read. `coverage report` stays the gate.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PACKAGE_GLOB = "src/dsl41/*"


def branch_rows(data: dict[str, Any]) -> list[tuple[str, int, int]]:
    """`(path, covered, total)` per file, most missed branches first.

    Ties break by path so the order is stable."""
    rows = [
        (path, int(entry["summary"]["covered_branches"]), int(entry["summary"]["num_branches"]))
        for path, entry in data["files"].items()
    ]
    return sorted(rows, key=lambda row: (-(row[2] - row[1]), row[0]))


def render(rows: list[tuple[str, int, int]]) -> str:
    """The table: one line per module, then the total."""
    width = max((len(path) for path, _, _ in rows), default=len("TOTAL"))
    width = max(width, len("TOTAL"))
    lines = [f"{'module':<{width}}  {'covered':>7}  {'branches':>8}  {'missed':>6}  {'pct':>7}"]
    for path, covered, total in rows:
        lines.append(_line(path, covered, total, width))
    lines.append(
        _line("TOTAL", sum(c for _, c, _ in rows), sum(t for _, _, t in rows), width),
    )
    return "\n".join(lines)


def _line(name: str, covered: int, total: int, width: int) -> str:
    pct = f"{100 * covered / total:6.2f}%" if total else "   n/a "
    return f"{name:<{width}}  {covered:>7}  {total:>8}  {total - covered:>6}  {pct:>7}"


def coverage_json() -> dict[str, Any]:
    """Coverage's JSON for every module under src/dsl41.

    `--include` replaces the configured include list, and `--fail-under=0`
    keeps the gate's threshold out of a report."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "coverage.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                "json",
                f"--include={PACKAGE_GLOB}",
                "--fail-under=0",
                "-q",
                "-o",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            sys.stderr.write(result.stdout + result.stderr)
            raise SystemExit(
                "coverage json failed; run `coverage run -m pytest -q` and `coverage combine` first"
            )
        loaded: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
        return loaded


def main() -> int:
    print(render(branch_rows(coverage_json())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
