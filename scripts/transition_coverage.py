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

Exit 0 when every unmarked transition was hit, 1 on any miss, on any recorded
violation, or when a marked transition (`spec-only` or `unreachable`) was hit
(the mark is stale or wrong), and 2 when the hits file is missing.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dsl41.machines import MACHINES
from dsl41.state_machine import StateMachine, Transition, state_name

ROOT = Path(__file__).resolve().parents[1]
HITS_FILE = ROOT / ".transition-hits.json"


def hit_sources(hits: Iterable[dict[str, str]]) -> dict[tuple[str, str], set[str]]:
    """(machine, transition id) to the old states a test took it from."""
    seen: dict[tuple[str, str], set[str]] = {}
    for hit in hits:
        seen.setdefault((hit["machine"], hit["id"]), set()).add(hit["old"])
    return seen


def describe(machine: StateMachine[Any], t: Transition[Any]) -> str:
    sources = ", ".join(sorted(state_name(s) for s in t.source))
    members = ", ".join(sorted(state_name(s) for s in t.targets()))
    target = "{" + members + "}" if isinstance(t.target, frozenset) else members
    return f"{machine.name} {t.id} {{{sources}}} --{t.trigger}--> {target}"


def report(
    machines: Iterable[StateMachine[Any]],
    hits: Iterable[dict[str, str]],
    violations: Iterable[dict[str, str]] = (),
) -> tuple[list[str], int]:
    """The report lines and the exit status for these machines, hits and violations."""
    seen = hit_sources(hits)
    summary: list[str] = []
    misses: list[str] = []
    marked: list[str] = []
    partial: list[str] = []
    wrong_marks: list[str] = []
    for machine in machines:
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


def main(hits_file: Path = HITS_FILE, machines: Iterable[StateMachine[Any]] | None = None) -> int:
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
