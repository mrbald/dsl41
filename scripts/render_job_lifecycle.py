"""Write the lifecycle views: `docs/job-chart-lifecycle.md` and `docs/chart-lifecycles.md`.

Run it after a chart changes:

    uv run python scripts/render_job_lifecycle.py                      # writes both files
    uv run python scripts/render_job_lifecycle.py --full               # job, with folded rows
    uv run python scripts/render_job_lifecycle.py --chart host --full  # one chart, the same

`tests/test_job_lifecycle_doc.py` fails when a committed file and the rendering differ.

Each file is a projection of the declared charts in `dsl41.machines.MACHINES`, not a second
source. One line per transition, `Target = Source + Event [guard] // note`; a further way into
the same target is a continuation line that starts with `||`. Five kinds of row are folded
into counts. Ignored rows, rows marked unreachable, refusal rows and internal rows move nothing.
A self-transition leaves its state and enters it again. Folded plus shown equals the chart's
rows, and the rendering refuses to finish when it does not.

The job file has the job chart's own layout. The lifecycle holds the rows that move the
`status` region, the modes the rows that move a flag, and the holding the rows that move the
`holding` region. Its blocks follow `READING_ORDER`. Its counts and its `--full` headings put
the refusal rows with the internal rows.

The other file has one section per other chart, in `MACHINES` order, and its counts line first.
A chart's view has a block per top-level region. An orthogonal composite, one with two or more
regions, has a block of its own under its name. A composite with one region groups states
inside its parent's block, so a grouping state never splits the ways into one target. That
block names each such composite's members on a line `group = {member, member}`. The
region a row moves is the innermost one that holds its source and every target, a containing
endpoint included (DL-325; statechart-engine.md §4 for the exit and entry order). A row is in
the block that holds that region. A row between two regions of one composite is refused, as
`well_formed` refuses it (§3). A row from the chart's root leaves every region and enters each
again; its line is in the block of its targets' top-level region. Blocks follow the declaration
order, parents first, unless `READING_ORDERS` gives the chart one.

A row's note is the row's `note`. It is printed only when the whole line fits in `LINE`
columns. A note that names a row id (`job.196`, or the short `.10`) is dropped: the view
prints no ids, so the reader could not follow it. A refusal row's note is its code. A row
marked spec-only says so whatever its length. The cite stays out.

The ways into a target are sorted by source, in the declaration order of the states with a
group before its members, then by event, then by guard. A row that goes to a choice prints one
line per branch, the branch guard after the row guard and a semicolon.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

from dsl41.job_chart import JOB
from dsl41.machines import MACHINES
from dsl41.state_machine import Region, Row, State, Statechart

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "job-chart-lifecycle.md"
CHARTS_DOC = ROOT / "docs" / "chart-lifecycles.md"

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

#: A chart's reading order, by chart name; a chart not named here reads in declaration order.
READING_ORDERS: dict[str, tuple[str, ...]] = {JOB.name: READING_ORDER}

#: The charts of `docs/chart-lifecycles.md`: every registered chart but `job`, in order.
OTHERS = tuple(chart for chart in MACHINES if chart.name != JOB.name)

#: A row id in a note: a chart's name and a number (`job.196`), or the short form (`.10`).
ROW_ID = re.compile(r"(?<![\w.])(?:[a-z][a-z_]*)?\.\d{2,3}(?!\w|\.\w)")


@dataclass(frozen=True)
class Edge:
    """One way into a state: a row, or one branch of the choice a row goes to.

    An empty `target` is a row that moves nothing. `row` is the row's id, which the file does
    not print; it lets a count be of rows, not of branches. `mark` is printed on every line.
    """

    row: str
    target: str
    source: str
    event: str
    guard: str
    note: str
    mark: str = ""


@dataclass
class View:
    """The rows of a chart sorted into what the file shows and what it folds.

    `shown` is keyed by the block a row is printed in: a view region of the job layout, or
    the index of a `Block` of the chart layout.
    """

    shown: dict[str, list[Edge]] = field(default_factory=dict)
    ignored: list[Edge] = field(default_factory=list)
    unreachable: list[Edge] = field(default_factory=list)
    refusals: list[Edge] = field(default_factory=list)
    internal: list[Edge] = field(default_factory=list)
    selves: list[Edge] = field(default_factory=list)

    def shown_rows(self) -> int:
        return _rows([edge for edges in self.shown.values() for edge in edges])

    def folded(self) -> tuple[list[Edge], ...]:
        return (self.ignored, self.unreachable, self.refusals, self.internal, self.selves)


@dataclass(frozen=True)
class Block:
    """One block of a chart's view: a top-level region, or an orthogonal composite's regions.

    `title` is the region's name or the composite's.
    """

    title: str
    regions: tuple[Region, ...]
    composite: bool


# ---- the chart's regions


def _subtree(state: State) -> list[State]:
    """The state and every state under it, parents first, in declaration order."""
    found = [state]
    for region in state.regions:
        for sub in region.states:
            found += _subtree(sub)
    return found


def view_regions(chart: Statechart) -> dict[str, Region]:
    """The regions the job view names: `status`, `holding` and each region inside `flags`."""
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


def _inline(region: Region) -> list[State]:
    """The states a region's block holds, parents first: its states, and those of each
    composite with one region below them. An orthogonal composite is held; its regions are not."""
    found: list[State] = []
    for state in region.states:
        found.append(state)
        if len(state.regions) == 1:
            found += _inline(state.regions[0])
    return found


def blocks_of(chart: Statechart) -> list[Block]:
    """The chart view's blocks: each top-level region, then each orthogonal composite inside
    it, parents first, in declaration order."""
    found: list[Block] = []

    def add(title: str, regions: tuple[Region, ...], composite: bool) -> None:
        found.append(Block(title, regions, composite))
        for region in regions:
            for state in _inline(region):
                if len(state.regions) > 1:
                    add(state.name, state.regions, True)

    for region in chart.root.regions:
        add(region.name, (region,), False)
    return found


def _block_of(blocks: Sequence[Block]) -> dict[int, str]:
    """The key of the block that holds each region, by the region's `id`."""
    held: dict[int, str] = {}
    for index, part in enumerate(blocks):
        for region in part.regions:
            held[id(region)] = str(index)
            for state in _inline(region):
                if len(state.regions) == 1:
                    held[id(state.regions[0])] = str(index)
    return held


def _entered(region: Region) -> str:
    """The state a region's entry rests in within its block: its initial, then down through
    each composite with one region."""
    while True:
        state = next(s for s in region.states if s.name == region.initial)
        if len(state.regions) != 1:
            return state.name
        region = state.regions[0]


def _enclosing(chart: Statechart) -> tuple[dict[str, tuple[Region, ...]], dict[int, str]]:
    """Each state's enclosing regions, outermost first, the root's empty; and each region's
    owning state, by the region's `id`."""
    paths: dict[str, tuple[Region, ...]] = {}
    owners: dict[int, str] = {}

    def walk(state: State, path: tuple[Region, ...]) -> None:
        paths[state.name] = path
        for region in state.regions:
            owners[id(region)] = state.name
            for sub in region.states:
                walk(sub, (*path, region))

    walk(chart.root, ())
    return paths, owners


def _crossing(chain: tuple[Region, ...], other: tuple[Region, ...], owners: dict[int, str]) -> bool:
    """True when two states lie in two regions of one composite (statechart-engine.md §3)."""
    for mine, theirs in zip(chain, other, strict=False):
        if mine is not theirs:
            return owners[id(mine)] == owners[id(theirs)]
    return False


def _moved(
    chart: Statechart,
    enclosing: tuple[dict[str, tuple[Region, ...]], dict[int, str]],
    row: Row,
    edges: Sequence[Edge],
) -> Region:
    """The region a row moves: the innermost one holding its source and every target, a
    containing endpoint included (DL-325; statechart-engine.md §4 for the exit and entry order).
    A row between two regions of one composite is refused, as `well_formed` refuses it (§3). A
    row from the root leaves every region; the view places it in its targets' top-level
    region."""
    paths, owners = enclosing
    ends = [edge.target for edge in edges]
    if row.source != chart.root.name:
        source = paths[row.source]
        if any(_crossing(source, paths[end], owners) for end in ends):
            raise ValueError(f"{row.id}: source and target lie in two regions of one composite")
        ends.append(row.source)
    chains = [paths[name] for name in ends]
    common = 0
    while all(len(chain) > common for chain in chains) and all(
        chain[common] is chains[0][common] for chain in chains
    ):
        common += 1
    if row.source == chart.root.name:
        common = min(common, 1)
    if common == 0:
        raise ValueError(f"{row.id}: no single top-level region holds source and target")
    return chains[0][common - 1]


# ---- the rows


def _note(row: Row) -> str:
    """The note a line may carry, or empty."""
    if row.kind == "refusal":
        return f"refusal {row.code}"
    text = " ".join(row.note.split()).rstrip(".")
    return "" if ROW_ID.search(text) else text


def _edges(chart: Statechart, row: Row) -> list[Edge]:
    """One edge for a row, or one per branch for a row that goes to a choice."""
    event = row.event or "(completion)"
    mark = "spec-only" if row.mark == "spec-only" else ""
    choices = {choice.name: choice for choice in chart.choices}
    if row.target not in choices:
        note = _note(row)
        return [Edge(row.id, row.target, row.source, event, row.guard, note, mark)]
    edges = []
    for branch in choices[row.target].branches:
        guard = "; ".join(part for part in (row.guard, branch.guard or "else") if part)
        note = _note(row)
        edges.append(Edge(row.id, branch.target, row.source, event, guard, note, mark))
    return edges


def _fold(row: Row, edges: list[Edge], view: View) -> bool:
    """Count a row that moves nothing into its folded kind; False for a row the view shows."""
    if row.mark == "unreachable":
        view.unreachable += edges
    elif row.kind == "ignored":
        view.ignored += edges
    elif row.kind == "refusal":
        view.refusals += edges
    elif row.kind != "external":
        view.internal += edges
    elif row.target == row.source:
        view.selves += edges
    else:
        return False
    return True


def _sort(chart: Statechart, view: View, order: dict[str, int]) -> View:
    """Sort each block by target, in reading order, then by source, event and guard; and check
    that shown plus folded is the chart's rows."""
    rank = {name: index for index, name in enumerate(READING_ORDERS.get(chart.name, ()))}
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


def sort_rows(chart: Statechart) -> View:
    """Sort every row of the chart into the job view or into a folded count."""
    regions = view_regions(chart)
    placed = _placement(regions)
    view = View(shown={name: [] for name in regions})
    for row in chart.rows:
        edges = _edges(chart, row)
        if _fold(row, edges, view):
            continue
        homes = {placed.get(edge.target) for edge in edges} | {placed.get(row.source)}
        if len(homes) != 1 or None in homes:
            raise ValueError(f"{row.id}: no single region of the view holds source and target")
        view.shown[homes.pop() or ""] += edges
    return _sort(chart, view, _order(regions))


def sort_chart(chart: Statechart) -> View:
    """Sort every row of the chart into a block of the chart view or into a folded count."""
    blocks = blocks_of(chart)
    held = _block_of(blocks)
    view = View(shown={str(index): [] for index in range(len(blocks))})
    enclosing = _enclosing(chart)
    for row in chart.rows:
        edges = _edges(chart, row)
        if not _fold(row, edges, view):
            view.shown[held[id(_moved(chart, enclosing, row, edges))]] += edges
    order = {state.name: index for index, state in enumerate(_subtree(chart.root))}
    return _sort(chart, view, order)


def _rows(edges: Sequence[Edge]) -> int:
    """The number of rows the edges come from."""
    return len({edge.row for edge in edges})


def _folded(view: View) -> int:
    return sum(_rows(edges) for edges in view.folded())


# ---- the lines


def _line(prefix: str, edge: Edge) -> str:
    """One transition; the note is added only when the line stays within `LINE`."""
    core = f"{prefix}{edge.source} + {edge.event}"
    if edge.guard:
        core += f" [{edge.guard}]"
    tail = "; ".join(part for part in (edge.mark, edge.note) if part)
    with_note = f"{core} // {tail}"
    if tail and len(with_note) <= LINE:
        return with_note
    return f"{core} // {edge.mark}" if edge.mark else core


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


def _folded_sections(view: View, level: str, merged: dict[str, int] | None = None) -> list[str]:
    """The folded rows under their own headings, for `--full`; a kind with no rows has none.
    With `merged`, the rows' places in the chart, the refusal rows are among the internal rows
    in that order."""
    internal = view.internal
    refusals = view.refusals
    if merged is not None:
        internal = sorted(view.internal + view.refusals, key=lambda e: merged[e.row])
        refusals = []
    sections = (
        ("Ignored", view.ignored, False),
        ("Marked unreachable", view.unreachable, False),
        ("Refusals", refusals, False),
        ("Internal", internal, False),
        ("Self-transitions", view.selves, True),
    )
    lines: list[str] = []
    for title, edges, targeted in sections:
        if not edges:
            continue
        body = _blocks(edges) if targeted else _plain(edges)
        lines += ["", f"{level} {title}", "", *_fence(body)]
    return lines


def counts(chart: Statechart) -> list[str]:
    """The job view's folded rows, one count per line, and the sum that ties them to the chart.
    A refusal row is counted with the internal rows: neither moves the configuration."""
    view = sort_rows(chart)
    shown = view.shown_rows()
    lines = [
        f"- Ignored rows: {_rows(view.ignored)}. The event is consumed and nothing moves.",
        f"- Rows marked unreachable: {_rows(view.unreachable)}. No input reaches the cell.",
        f"- Internal rows: {_rows(view.internal) + _rows(view.refusals)}."
        " The configuration does not move: relays, moments and the arm's writes.",
        f"- Self-transitions: {_rows(view.selves)}. A state is left and entered again.",
        "",
        f"Shown: {shown}. Folded: {_folded(view)}. Rows in the chart: {len(chart.rows)}.",
    ]
    return lines


def render(chart: Statechart = JOB, *, full: bool = False) -> str:
    """The whole job file; with `full`, the folded rows too, under their own headings."""
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
        merged = {row.id: index for index, row in enumerate(chart.rows)}
        lines += _folded_sections(view, "##", merged)
    return "\n".join(lines) + "\n"


#: The folded kinds, in the order of `View.folded`.
KINDS = ("ignored", "marked unreachable", "refusals", "internal", "self-transitions")


def chart_counts(chart: Statechart) -> str:
    """A chart's counts line: the shown rows, the folded rows with each kind the chart has,
    and the chart's rows."""
    view = sort_chart(chart)
    kinds = ", ".join(
        f"{name} {_rows(edges)}" for name, edges in zip(KINDS, view.folded(), strict=True) if edges
    )
    folded = f"{_folded(view)} ({kinds})" if kinds else "0"
    return f"Shown: {view.shown_rows()}. Folded: {folded}. Rows in the chart: {len(chart.rows)}."


def _groups(part: Block) -> list[str]:
    """One `group = {member, member}` line per composite with one region that the block holds,
    in declaration order, parents first."""
    return [
        f"{state.name} = {{{', '.join(member.name for member in state.regions[0].states)}}}"
        for region in part.regions
        for state in _inline(region)
        if len(state.regions) == 1
    ]


def section(chart: Statechart, *, full: bool = False) -> list[str]:
    """One chart's section: its counts line, then its blocks under their top-level regions;
    with `full`, the folded rows too."""
    view = sort_chart(chart)
    lines = [f"## {chart.name}", "", chart_counts(chart)]
    for index, part in enumerate(blocks_of(chart)):
        heading = "####" if part.composite else "###"
        body = [f"{_entered(region)} <- initial" for region in part.regions]
        body += _groups(part)
        body += _blocks(view.shown[str(index)])
        lines += ["", f"{heading} {part.title}", "", *_fence(body)]
    if full:
        lines += _folded_sections(view, "###")
    return lines


def render_charts(charts: Sequence[Statechart] = OTHERS) -> str:
    """The whole file of the other charts' views."""
    lines = [
        "# Chart lifecycles",
        "",
        "This file is generated from `dsl41.machines`;",
        f"regenerate it with `{REGENERATE}`.",
        "It is a view of every registered chart but `job`, not the charts.",
        "[job-chart-lifecycle.md](job-chart-lifecycle.md) is the `job` chart's view, and",
        "[state-machines.md](state-machines.md) lists every row.",
        "",
        "One line per transition: `Target = Source + Event [guard] // note`. A line that starts",
        "with `||` is another way into the same target. A source may be a group of states.",
        "A source with the chart's own name is the chart's root. Its row leaves every region",
        "and enters each again, and its line is in the block of its target's top-level region.",
        "`State <- initial` names the state a region's entry rests in.",
        "",
        "A chart's section opens with its counts. Five kinds of row are folded into them.",
        "An ignored row consumes its event and moves nothing.",
        "No input reaches a row marked unreachable.",
        "A refusal row refuses its event and moves nothing.",
        "An internal row runs its action and moves nothing.",
        "A self-transition leaves its state and enters it again.",
        "`--full --chart NAME` prints the folded rows.",
        "",
        "A top-level region has a block. A composite with two or more regions has a block of",
        "its own under its name, after each region's initial state. A composite with one region",
        "groups states inside its parent's block. The block names its members on one line,",
        "`group = {member, member}`, after the initial states.",
    ]
    for chart in charts:
        lines += ["", *section(chart)]
    return "\n".join(lines) + "\n"


def is_current(root: pathlib.Path = ROOT, chart: Statechart = JOB) -> bool:
    """True when the committed job file at `root` equals the rendering."""
    doc = root / "docs" / "job-chart-lifecycle.md"
    return doc.exists() and doc.read_text(encoding="utf-8") == render(chart)


def charts_current(root: pathlib.Path = ROOT, charts: Sequence[Statechart] = OTHERS) -> bool:
    """True when the committed charts file at `root` equals the rendering."""
    doc = root / "docs" / "chart-lifecycles.md"
    return doc.exists() and doc.read_text(encoding="utf-8") == render_charts(charts)


def _write(doc: pathlib.Path, text: str) -> None:
    if doc.exists() and doc.read_text(encoding="utf-8") == text:
        print(f"{doc.name}: already current")
        return
    doc.write_text(text, encoding="utf-8")
    print(f"{doc.name}: written")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--full", action="store_true", help="print the folded rows too")
    parser.add_argument(
        "--chart",
        choices=[chart.name for chart in MACHINES],
        help="print this chart's view instead of writing the files",
    )
    args = parser.parse_args(argv)
    if args.full and args.chart is None:
        args.chart = JOB.name
    if args.chart == JOB.name:
        sys.stdout.write(render(full=args.full))
        return 0
    if args.chart is not None:
        chart = next(chart for chart in MACHINES if chart.name == args.chart)
        sys.stdout.write("\n".join(section(chart, full=args.full)) + "\n")
        return 0
    _write(DOC, render())
    _write(CHARTS_DOC, render_charts())
    return 0


if __name__ == "__main__":
    sys.exit(main())
