#!/usr/bin/env python3
"""The state-machine transition coverage gate.

Run it after `coverage run -m pytest -q`, which leaves `.transition-hits.json`
at the repository root (the pytest plugin in tests/transition_hits_plugin.py):

    uv run python scripts/transition_coverage.py

It reads the hits and `dsl41.machines.MACHINES`, and prints per statechart
`name: hit/total` over its required coverage items, then every item with no
hit, then the marked rows (listed, never counted).

A statechart (docs/statechart-engine.md §13) is gated on every class of its
coverage items at once: rows, choice branches, both outcomes of each guard,
and a defer and a release for each deferred event. All are held at 100%. A
guard outcome declared unreachable is listed as forbidden and fails the gate
when hit, and so is a marked row and its guard's true outcome: the row is
listed with the marked rows, never counted. A marked row's guard false is the
cell another row takes, neither required nor forbidden (DL-353).

Exit 0 when every required item was hit, 1 on any miss or when a forbidden
item was hit (the mark is stale or wrong), and 2 when the hits file is
missing.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path

from dsl41.machines import MACHINES
from dsl41.state_machine import Statechart, coverage_items

ROOT = Path(__file__).resolve().parents[1]
HITS_FILE = ROOT / ".transition-hits.json"


def chart_items(hits: Iterable[dict[str, str]]) -> set[tuple[str, str, str, str]]:
    """Every (machine, kind, id, detail) a statechart recorded."""
    return {(hit["machine"], hit["kind"], hit["id"], hit.get("detail", "")) for hit in hits}


def chart_report(
    chart: Statechart, items: set[tuple[str, str, str, str]]
) -> tuple[str, list[str], list[str], list[str]]:
    """The summary line, the misses, the forbidden hits and the marked rows of
    one statechart."""
    required, forbidden = coverage_items(chart)

    def line(item: tuple[str, str, str]) -> str:
        return " ".join(part for part in (chart.name, *item) if part)

    hit = [item for item in required if (chart.name, *item) in items]
    misses = [line(item) for item in required if (chart.name, *item) not in items]
    wrong = [line(item) for item in forbidden if (chart.name, *item) in items]
    marked = [
        f"{chart.name} {r.id} {{{r.source}}} --{r.event}--> {r.target or r.kind} [{r.mark}]"
        for r in chart.rows
        if r.mark is not None
    ]
    return f"{chart.name}: {len(hit)}/{len(required)}", misses, wrong, marked


def report(machines: Iterable[Statechart], hits: Iterable[dict[str, str]]) -> tuple[list[str], int]:
    """The report lines and the exit status for these machines and hits."""
    items = chart_items(hits)
    summary: list[str] = []
    misses: list[str] = []
    marked: list[str] = []
    wrong_marks: list[str] = []
    for machine in machines:
        line, chart_misses, chart_wrong, chart_marked = chart_report(machine, items)
        summary.append(line)
        misses += chart_misses
        wrong_marks += chart_wrong
        marked += chart_marked
    lines = list(summary)
    if misses:
        lines += ["", "Transitions with no hit:", *misses]
    if marked:
        lines += ["", "Marked transitions (excluded):", *marked]
    if wrong_marks:
        lines += ["", "Marked but hit (the mark is stale or wrong):", *wrong_marks]
    return lines, 1 if misses or wrong_marks else 0


def main(hits_file: Path = HITS_FILE, machines: Iterable[Statechart] | None = None) -> int:
    if not hits_file.is_file():
        print(f"{hits_file.name}: missing; run the test suite first", file=sys.stderr)
        return 2
    data = json.loads(hits_file.read_text(encoding="utf-8"))
    lines, status = report(MACHINES if machines is None else machines, data["hits"])
    print("\n".join(lines))
    return status


if __name__ == "__main__":
    sys.exit(main())
