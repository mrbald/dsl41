"""Write `docs/job-chart-lifecycle.md`: the job chart's lifecycle, flags and holding as plain lines.

Run it after the `job` chart changes:

    uv run python scripts/render_job_lifecycle.py          # writes the file
    uv run python scripts/render_job_lifecycle.py --full   # prints the folded rows too

`tests/test_job_lifecycle_doc.py` fails when the committed file and the rendering differ.

The file is a projection of `dsl41.job_chart.JOB`, not a second source. One line per
transition, `Target = Source + Event [guard] // note`; a further way into the same target is a
continuation line that starts with `||`. The lifecycle holds the rows that move the `status`
region, the modes the rows that move a flag, and the holding the rows that move the `holding`
region. Every other row is folded into one count: ignored rows, rows marked unreachable,
internal rows and self-transitions. Folded plus shown equals the chart's rows, and the
rendering refuses to finish when it does not.

A row's note is the row's `note`. It is printed only when the whole line fits in `LINE`
columns. A note that names a row id (`job.196`) is dropped: the view prints no ids, so the
reader could not follow it. A refusal row's note is its code. The cite stays out.

Blocks follow `READING_ORDER`. The ways into a target are sorted by source, in the declaration
order of the region's states with a group before its members, then by event, then by guard. A
row that goes to a choice prints one line per branch, the branch guard after the row guard and a
semicolon.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

from dsl41.job_chart import JOB
from dsl41.state_machine import Region, Row, State, Statechart

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "job-chart-lifecycle.md"

REGENERATE = "uv run python scripts/render_job_lifecycle.py"

#: The longest line a note may extend a transition to (ruff's line length).
LINE = 100

STATUS = "status"
FLAGS = "flags"
HOLDING = "holding"

#: The order a reader meets the statuses: the way into a job, the wait, the start, the run and
#: its box's completion door, then the ends. The declaration's order is the status tree's, which
#: this is not; the reading order is the lifecycle's and is written here. A target the tuple
#: does not name follows in declaration order.
READING_ORDER = (
    "INACTIVE",
    "QUE_WAIT",
    "ADMITTED",
    "STARTING",
    "RUNNING",
    "overriding",
    "folding",
    "settled",
    "SUCCESS",
    "FAILURE",
    "TERMINATED",
)


@dataclass(frozen=True)
class Edge:
    """One way into a state: a row, or one branch of the choice a row goes to.

    An empty `target` is a row that moves nothing. `row` is the row's id, which the file does
    not print; it lets a count be of rows, not of branches.
    """

    row: str
    target: str
    source: str
    event: str
    guard: str
    note: str


@dataclass
class View:
    """The rows of a chart sorted into what the file shows and what it folds."""

    shown: dict[str, list[Edge]] = field(default_factory=dict)
    ignored: list[Edge] = field(default_factory=list)
    unreachable: list[Edge] = field(default_factory=list)
    internal: list[Edge] = field(default_factory=list)
    selves: list[Edge] = field(default_factory=list)

    def shown_rows(self) -> int:
        return _rows([edge for edges in self.shown.values() for edge in edges])


# ---- the chart's regions


def _subtree(state: State) -> list[State]:
    """The state and every state under it, parents first, in declaration order."""
    found = [state]
    for region in state.regions:
        for sub in region.states:
            found += _subtree(sub)
    return found


def view_regions(chart: Statechart) -> dict[str, Region]:
    """The regions the view names: `status`, `holding` and each region inside `flags`."""
    top = {region.name: region for region in chart.root.regions}
    if STATUS not in top or FLAGS not in top or HOLDING not in top:
        raise ValueError(f"{chart.name}: the view needs the regions {STATUS}, {FLAGS}, {HOLDING}")
    inner = {region.name: region for state in top[FLAGS].states for region in state.regions}
    return {STATUS: top[STATUS], **inner, HOLDING: top[HOLDING]}


def _placement(regions: dict[str, Region]) -> dict[str, str]:
    """Each state's view region, by state name."""
    placed: dict[str, str] = {}
    for name, region in regions.items():
        for state in _states_of(region):
            placed[state.name] = name
    return placed


def _order(regions: dict[str, Region]) -> dict[str, int]:
    """A state's place in declaration order, parents first, for ordering the blocks."""
    names = [s.name for r in regions.values() for s in _states_of(r)]
    return {name: index for index, name in enumerate(names)}


def initial_state(region: Region) -> str:
    """The leaf the region's initial pseudostate reaches."""
    while True:
        state = next(s for s in _states_of(region) if s.name == region.initial)
        if not state.regions:
            return state.name
        region = state.regions[0]


def _states_of(region: Region) -> list[State]:
    return [s for top in region.states for s in _subtree(top)]


# ---- the rows


def _note(chart: Statechart, row: Row) -> str:
    """The note a line may carry, or empty."""
    if row.kind == "refusal":
        return f"refusal {row.code}"
    text = " ".join(row.note.split()).rstrip(".")
    names_a_row = re.search(rf"(?<![\w.]){re.escape(chart.name)}\.\d+", text)
    return "" if names_a_row else text


def _edges(chart: Statechart, row: Row) -> list[Edge]:
    """One edge for a row, or one per branch for a row that goes to a choice."""
    event = row.event or "(completion)"
    choices = {choice.name: choice for choice in chart.choices}
    if row.target not in choices:
        note = _note(chart, row)
        return [Edge(row.id, row.target, row.source, event, row.guard, note)]
    edges = []
    for branch in choices[row.target].branches:
        guard = "; ".join(part for part in (row.guard, branch.guard or "else") if part)
        note = _note(chart, row)
        edges.append(Edge(row.id, branch.target, row.source, event, guard, note))
    return edges


def sort_rows(chart: Statechart) -> View:
    """Sort every row of the chart into the view or into a folded count."""
    regions = view_regions(chart)
    placed = _placement(regions)
    order = _order(regions)
    view = View(shown={name: [] for name in regions})
    for row in chart.rows:
        edges = _edges(chart, row)
        if row.mark == "unreachable":
            view.unreachable += edges
        elif row.kind == "ignored":
            view.ignored += edges
        elif row.kind != "external":
            view.internal += edges
        elif row.target == row.source:
            view.selves += edges
        else:
            homes = {placed.get(edge.target) for edge in edges} | {placed.get(row.source)}
            if len(homes) != 1 or None in homes:
                raise ValueError(f"{row.id}: no single region of the view holds source and target")
            view.shown[homes.pop() or ""] += edges
    rank = {name: index for index, name in enumerate(READING_ORDER)}
    for edges in view.shown.values():
        edges.sort(
            key=lambda e: (
                (rank.get(e.target, len(rank) + order[e.target]), order[e.source])
                + (e.event, e.guard)
            )
        )
    if view.shown_rows() + _folded(view) != len(chart.rows):
        raise ValueError("the view and its folded rows do not add up to the chart's rows")
    return view


def _rows(edges: Sequence[Edge]) -> int:
    """The number of rows the edges come from."""
    return len({edge.row for edge in edges})


def _folded(view: View) -> int:
    return sum(
        _rows(edges) for edges in (view.ignored, view.unreachable, view.internal, view.selves)
    )


# ---- the lines


def _line(prefix: str, edge: Edge) -> str:
    """One transition; the note is added only when the line stays within `LINE`."""
    core = f"{prefix}{edge.source} + {edge.event}"
    if edge.guard:
        core += f" [{edge.guard}]"
    with_note = f"{core} // {edge.note}"
    return with_note if edge.note and len(with_note) <= LINE else core


def block(target: str, edges: Sequence[Edge]) -> list[str]:
    """The ways into one target: `Target = ...`, then `||` lines under the `=`."""
    lead = f"{target} = "
    more = " " * len(target) + "|| "
    return [_line(lead if index == 0 else more, edge) for index, edge in enumerate(edges)]


def _blocks(edges: Sequence[Edge]) -> list[str]:
    """Blocks by target, in the order the edges are sorted."""
    lines: list[str] = []
    targets = list(dict.fromkeys(edge.target for edge in edges))
    for target in targets:
        lines += block(target, [edge for edge in edges if edge.target == target])
    return lines


def _plain(edges: Sequence[Edge]) -> list[str]:
    return [_line("", edge) for edge in edges]


def _fence(lines: Sequence[str]) -> list[str]:
    return ["```text", *lines, "```"]


def counts(chart: Statechart) -> list[str]:
    """The folded rows, one count per line, and the sum that ties them to the chart."""
    view = sort_rows(chart)
    shown = view.shown_rows()
    lines = [
        f"- Ignored rows: {_rows(view.ignored)}. The event is consumed and nothing moves.",
        f"- Rows marked unreachable: {_rows(view.unreachable)}. No input reaches the cell.",
        f"- Internal rows: {_rows(view.internal)}."
        " The configuration does not move: relays, moments and the arm's writes.",
        f"- Self-transitions: {_rows(view.selves)}. A state is left and entered again.",
        "",
        f"Shown: {shown}. Folded: {_folded(view)}. Rows in the chart: {len(chart.rows)}.",
    ]
    return lines


def render(chart: Statechart = JOB, *, full: bool = False) -> str:
    """The whole file; with `full`, the folded rows too, under their own headings."""
    regions = view_regions(chart)
    view = sort_rows(chart)
    initial = {name: initial_state(region) for name, region in regions.items()}
    lines = [
        "# Job chart lifecycle",
        "",
        "This file is generated from `dsl41.job_chart`;",
        f"regenerate it with `{REGENERATE}`.",
        "It is a view of the `job` chart, not the chart. [state-machines.md](state-machines.md)",
        "lists every row.",
        "",
        "One line per transition: `Target = Source + Event [guard] // note`. A line that starts",
        "with `||` is another way into the same target. A source may be a group of states.",
        "Where a state inside a group source declares its own row on the same event, that row",
        "comes first, and it is among the folded rows.",
        "",
        "## Lifecycle",
        "",
        *_fence([f"{initial[STATUS]} <- initial", *_blocks(view.shown[STATUS])]),
        "",
        "## Modes",
    ]
    for name in regions:
        if name in (STATUS, HOLDING):
            continue
        body = [f"{initial[name]} <- initial", *_blocks(view.shown[name])]
        lines += ["", f"### {name}", "", *_fence(body)]
    lines += [
        "",
        "## Holding",
        "",
        *_fence([f"{initial[HOLDING]} <- initial", *_blocks(view.shown[HOLDING])]),
        "",
        "## Folded away",
        "",
        *counts(chart),
    ]
    if full:
        sections = (
            ("Ignored", view.ignored, False),
            ("Marked unreachable", view.unreachable, False),
            ("Internal", view.internal, False),
            ("Self-transitions", view.selves, True),
        )
        for title, edges, targeted in sections:
            body = _blocks(edges) if targeted else _plain(edges)
            lines += ["", f"## {title}", "", *_fence(body)]
    return "\n".join(lines) + "\n"


def is_current(root: pathlib.Path = ROOT, chart: Statechart = JOB) -> bool:
    """True when the committed file at `root` equals the rendering."""
    doc = root / "docs" / "job-chart-lifecycle.md"
    return doc.exists() and doc.read_text(encoding="utf-8") == render(chart)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--full", action="store_true", help="print the folded rows too")
    args = parser.parse_args(argv)
    if args.full:
        sys.stdout.write(render(full=True))
        return 0
    if is_current():
        print(f"{DOC.name}: already current")
        return 0
    DOC.write_text(render(), encoding="utf-8")
    print(f"{DOC.name}: written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
