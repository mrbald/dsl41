#!/usr/bin/env python3
"""The state-machine transition coverage gate.

Run it after `coverage run -m pytest -q`, which leaves `.transition-hits.json`
at the repository root (the pytest plugin in tests/transition_hits_plugin.py):

    uv run python scripts/transition_coverage.py

It reads the hits and `dsl41.machines.MACHINES`, and prints per machine
`name: hit/total` over the unmarked transitions, then every unmarked transition
with no hit, then the marked transitions (listed, never counted), then a
per-source view: for each hit transition, the source states no test took it
from. That last view is a report only.

A statechart (docs/statechart-engine.md §13) is gated on every class of its
coverage items at once: rows, choice branches, both outcomes of each guard,
and a defer and a release for each deferred event. All are held at 100%. A
guard outcome declared unreachable is listed as forbidden and fails the gate
when hit, and so is a marked row and its guard's true outcome: the row is
listed with the marked transitions, never counted. A marked row's guard false
is the cell another row takes, neither required nor forbidden (DL-353).

Exit 0 when every unmarked transition and every statechart item was hit, 1 on
any miss, on any recorded violation, or when a marked transition (`spec-only`
or `unreachable`) or a forbidden item was hit (the mark is stale or wrong), and
2 when the hits file is missing.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dsl41.machines import MACHINES
from dsl41.state_machine import (
    StateMachine,
    Statechart,
    Transition,
    coverage_items,
    is_statechart,
    state_name,
)

ROOT = Path(__file__).resolve().parents[1]
HITS_FILE = ROOT / ".transition-hits.json"


def hit_sources(hits: Iterable[dict[str, str]]) -> dict[tuple[str, str], set[str]]:
    """(machine, transition id) to the old states a test took it from.

    A statechart's hits carry a `kind` and no `old`; they are read by
    `chart_items` instead."""
    seen: dict[tuple[str, str], set[str]] = {}
    for hit in hits:
        if "kind" not in hit:
            seen.setdefault((hit["machine"], hit["id"]), set()).add(hit["old"])
    return seen


def chart_items(hits: Iterable[dict[str, str]]) -> set[tuple[str, str, str, str]]:
    """Every (machine, kind, id, detail) a statechart recorded."""
    return {
        (hit["machine"], hit["kind"], hit["id"], hit.get("detail", ""))
        for hit in hits
        if "kind" in hit
    }


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


def describe(machine: StateMachine[Any], t: Transition[Any]) -> str:
    sources = ", ".join(sorted(state_name(s) for s in t.source))
    members = ", ".join(sorted(state_name(s) for s in t.targets()))
    target = "{" + members + "}" if isinstance(t.target, frozenset) else members
    return f"{machine.name} {t.id} {{{sources}}} --{t.trigger}--> {target}"


def report(
    machines: Iterable[StateMachine[Any] | Statechart],
    hits: Iterable[dict[str, str]],
    violations: Iterable[dict[str, str]] = (),
) -> tuple[list[str], int]:
    """The report lines and the exit status for these machines, hits and violations."""
    hits = list(hits)
    seen = hit_sources(hits)
    items = chart_items(hits)
    summary: list[str] = []
    misses: list[str] = []
    marked: list[str] = []
    partial: list[str] = []
    wrong_marks: list[str] = []
    for machine in machines:
        if is_statechart(machine):
            line, chart_misses, chart_wrong, chart_marked = chart_report(machine, items)
            summary.append(line)
            misses += chart_misses
            wrong_marks += chart_wrong
            marked += chart_marked
            continue
        unmarked = [t for t in machine.transitions if t.mark is None]
        hit = [t for t in unmarked if (machine.name, t.id) in seen]
        summary.append(f"{machine.name}: {len(hit)}/{len(unmarked)}")
        for t in machine.transitions:
            if t.mark is not None:
                marked.append(f"{describe(machine, t)} [{t.mark}]")
                if (machine.name, t.id) in seen:
                    wrong_marks.append(f"{describe(machine, t)} [{t.mark}]")
            elif (machine.name, t.id) not in seen:
                misses.append(describe(machine, t))
            else:
                for state in sorted({state_name(s) for s in t.source} - seen[(machine.name, t.id)]):
                    partial.append(f"{machine.name} {t.id} from {state}")
    lines = list(summary)
    if misses:
        lines += ["", "Transitions with no hit:", *misses]
    if marked:
        lines += ["", "Marked transitions (excluded):", *marked]
    if partial:
        lines += ["", "Sources never exercised (report only):", *partial]
    if wrong_marks:
        lines += ["", "Marked but hit (the mark is stale or wrong):", *wrong_marks]
    broken = [
        f"{v.get('machine', '?')} {v.get('id', '?')}: {v.get('old', '')} -> "
        f"{v.get('new', '')}: {v.get('reason', '')}"
        for v in violations
    ]
    if broken:
        lines += ["", "Violations recorded:", *broken]
    return lines, 1 if misses or wrong_marks or broken else 0


def main(
    hits_file: Path = HITS_FILE, machines: Iterable[StateMachine[Any] | Statechart] | None = None
) -> int:
    if not hits_file.is_file():
        print(f"{hits_file.name}: missing; run the test suite first", file=sys.stderr)
        return 2
    data = json.loads(hits_file.read_text(encoding="utf-8"))
    lines, status = report(
        MACHINES if machines is None else machines, data["hits"], data.get("violations", [])
    )
    print("\n".join(lines))
    return status


if __name__ == "__main__":
    sys.exit(main())
