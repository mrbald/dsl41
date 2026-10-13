"""Generated estates and scripts for the old-vs-new oracle harness (DL-338).

One generator builds both a catalog, as JIL text, and a script over it. It
draws every choice through a `Chooser`, which has two implementations:

- `RandomChooser` draws from a seeded `random.Random`, so a seed and a size
  give the same case on every run. The harness's command line and its fixed
  sample use it.
- `DrawChooser` draws from hypothesis, so a failing case shrinks: fewer jobs,
  fewer inputs, and the first option of every choice. `cases()` is the
  strategy. Each job and each input is one hypothesis span, and an input
  takes at most five draws, so the shrinker can delete either whole.

Every option list puts its simplest member first, because hypothesis shrinks
toward it. Every name is invented.

The catalog has machines with and without a load cap, renewable, depletable
and threshold resources, globals, standard and extended calendars, and boxes
nested up to three deep. Jobs carry conditions over status, exit code and
global atoms, some with a lookback; run windows, start times, calendars and
time zones; resources, load and priority; box_success, box_failure,
box_terminator, job_terminator, auto_hold, term_run_time and must times;
start_times or start_mins; and an initial ice, hold or noexec status. Most
conditions point back to jobs made earlier; a few point forward or to the job itself, so the
condition graph has loops. The base instant is a summer day or the
eve of a DST change, so windows and must times cross one.

A script is a list of inputs: an `Event` to feed, an `Advance` that only
moves time, so due timers fire, and a `SealPoint` where the harness compares
the seal digests.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, TypeVar

from hypothesis import strategies as st

from dsl41.conditions import GlobalAtom, iter_atoms
from dsl41.ir import CatalogIR
from dsl41.oracle_state import Event, EventKind

T = TypeVar("T")


class Chooser(Protocol):
    """The one source of choices the generator reads."""

    def integer(self, low: int, high: int) -> int:
        """An integer in `[low, high]`."""
        ...

    def choice(self, options: Sequence[T]) -> T:
        """One of `options`; the first is the simplest."""
        ...

    def chance(self, percent: int) -> bool:
        """True with about `percent` in a hundred; shrinks to False."""
        ...

    def items(
        self, low: int, high: int, build: Callable[[Chooser, int], T], percent: int = 85
    ) -> list[T]:
        """`low` to `high` items, each made by `build(chooser, index)`. Under
        hypothesis each item past `low` is drawn as one span that goes on
        with `percent` in a hundred, so a shrink drops an item whole."""
        ...


class RandomChooser:
    """Choices from a seeded generator: deterministic per seed."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def integer(self, low: int, high: int) -> int:
        return self._rng.randint(low, high)

    def choice(self, options: Sequence[T]) -> T:
        return options[self._rng.randrange(len(options))]

    def chance(self, percent: int) -> bool:
        return self._rng.randrange(100) < percent

    def items(
        self, low: int, high: int, build: Callable[[Chooser, int], T], percent: int = 85
    ) -> list[T]:
        return [build(self, index) for index in range(self.integer(low, high))]


class DrawChooser:
    """Choices from hypothesis, so a counterexample shrinks."""

    def __init__(self, draw: st.DrawFn) -> None:
        self._draw = draw

    def integer(self, low: int, high: int) -> int:
        return self._draw(st.integers(min_value=low, max_value=high))

    def choice(self, options: Sequence[T]) -> T:
        return options[self._draw(st.integers(min_value=0, max_value=len(options) - 1))]

    def chance(self, percent: int) -> bool:
        return self._draw(st.integers(min_value=0, max_value=99)) >= 100 - percent

    def items(
        self, low: int, high: int, build: Callable[[Chooser, int], T], percent: int = 85
    ) -> list[T]:
        made: list[T] = []
        while len(made) < high:
            item = self._draw(_item(build, len(made), len(made) < low, percent))
            if item is None:
                break
            made.append(item)
        return made


@st.composite
def _item(
    draw: st.DrawFn, build: Callable[[Chooser, int], T], index: int, required: bool, percent: int
) -> T | None:
    """One item as one hypothesis span: None ends the list."""
    chooser = DrawChooser(draw)
    if not required and not chooser.chance(percent):
        return None
    return build(chooser, index)


@dataclass(frozen=True)
class Advance:
    """A time observation with no attempt: due timers fire (`Oracle.advance`)."""

    at: datetime


@dataclass(frozen=True)
class SealPoint:
    """Where the script asks for the seal digests to be compared."""


Step = Event | Advance | SealPoint


@dataclass(frozen=True)
class Case:
    """One generated estate and its script."""

    label: str
    jil: str
    script: tuple[Step, ...]
    jobs: int


#: The base instants: a summer day, the eve of the US spring change
#: (2026-03-08) and the eve of the EU autumn change (2026-10-25).
BASES = (
    datetime(2026, 7, 1, 8, 0),
    datetime(2026, 3, 7, 20, 0),
    datetime(2026, 10, 24, 20, 0),
)
ZONES = ("America/New_York", "Europe/London", "Asia/Tokyo")
STATUSES = ("SUCCESS", "FAILURE", "SUCCESS", "TERMINATED", "INACTIVE", "RUNNING", "STARTING")
OOB = ("ON_ICE", "OFF_ICE", "ON_HOLD", "OFF_HOLD", "ON_NOEXEC", "OFF_NOEXEC", "DISARM")
ADVANCE_MINUTES = (1, 5, 30, 90, 240, 600, 1440)


@dataclass
class _Job:
    name: str
    box: bool
    parent: str | None
    lines: list[str]
    resources: bool = False
    condition: str | None = None
    #: a condition atom on the job `offset` places on from this one (0 is
    #: itself), with `@` for its target; drawn with the job, placed once
    #: every job exists
    forward: tuple[int, str] | None = None


def _clock(minutes: int) -> str:
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def _calendar_lines(c: Chooser, name: str, base: datetime) -> list[str]:
    if c.chance(50):
        days = sorted({base.date() + timedelta(days=c.integer(-1, 3)) for _ in range(3)})
        return [f"calendar: {name}", *(f"{d:%m/%d/%Y} 00:00" for d in days)]
    week = int(base.strftime("%W")) + c.integer(-1, 1)
    anchor = c.choice(("mon", "wed", "fri"))
    return [
        f"extended_calendar: {name}",
        "workday: mo,tu,we,th,fr",
        f"condition: WEKR{anchor}#{week:02d}",
    ]


def _atom(c: Chooser, target: str, globals_: Sequence[str]) -> str:
    kind = c.choice(("s", "f", "d", "n", "t", "e", "value"))
    if kind == "value" and globals_:
        return f"value({c.choice(globals_)}) = {c.choice(('1', '0', 'go'))}"
    if kind in ("e", "value"):
        op = c.choice(("=", "!=", ">", "<="))
        return f"e({target}) {op} {c.integer(0, 3)}"
    if c.chance(15):
        return f"{kind}({target}, {c.choice(('00', '01', '04'))}.{c.choice(('00', '30'))})"
    return f"{kind}({target})"


def _condition(c: Chooser, targets: Sequence[str], globals_: Sequence[str]) -> str:
    atoms = c.items(1, 3, lambda c, _: _atom(c, c.choice(targets), globals_))
    text = atoms[0]
    for atom in atoms[1:]:
        text += f" {c.choice(('&', '|'))} {atom}"
    return text


def _ancestors(jobs: dict[str, _Job], name: str) -> set[str]:
    seen: set[str] = set()
    parent = jobs[name].parent
    while parent is not None:
        seen.add(parent)
        parent = jobs[parent].parent
    return seen


def _depth(jobs: dict[str, _Job], name: str) -> int:
    return len(_ancestors(jobs, name))


def estate(c: Chooser, size: int, label: str = "", min_size: int = 1) -> Case:
    """One estate of `min_size` to `size` jobs and a script over it."""
    base = c.choice(BASES)
    blocks: list[list[str]] = []

    def machine(c: Chooser, index: int) -> str:
        lines = [f"insert_machine: mach_{index}", "type: a", f"node_name: mach_{index}"]
        if c.chance(50):
            lines.append(f"max_load: {c.integer(1, 4)}")
        blocks.append(lines)
        return f"mach_{index}"

    def resource(c: Chooser, index: int) -> tuple[str, str]:
        kind = c.choice(("R", "D", "T"))
        blocks.append(
            [f"insert_resource: RES_{index}", f"res_type: {kind}", f"amount: {c.integer(1, 3)}"]
        )
        return f"RES_{index}", kind

    def global_(c: Chooser, index: int) -> str:
        blocks.append([f"insert_global: G_{index}", "value: 0"])
        return f"G_{index}"

    def calendar(c: Chooser, index: int) -> str:
        blocks.append(_calendar_lines(c, f"cal_{index}", base))
        return f"cal_{index}"

    machines = c.items(1, 3, machine)
    resources = dict(c.items(0, 3, resource))
    globals_ = c.items(0, 2, global_)
    calendars = c.items(0, 2, calendar)
    jobs: dict[str, _Job] = {}

    def job(c: Chooser, index: int) -> _Job:
        boxes = [n for n, j in jobs.items() if j.box and _depth(jobs, n) < 3]
        is_box = c.chance(15)
        parent = c.choice(boxes) if boxes and c.chance(50) else None
        name = f"{'box' if is_box else 'job'}_{index:03d}"
        lines = [f"insert_job: {name}", f"job_type: {'b' if is_box else 'c'}"]
        if not is_box:
            lines += [f"machine: {c.choice(machines)}", "command: /bin/true"]
        if parent is not None:
            lines.append(f"box_name: {parent}")
        made = jobs[name] = _Job(name, is_box, parent, lines)
        _job_attributes(c, made, jobs, base, resources, globals_, calendars)
        return made

    c.items(min_size, size, job)
    names = list(jobs)
    for position, made in enumerate(jobs.values()):
        # a forward edge, or one to itself: with the backward ones the jobs
        # draw for themselves, this closes condition loops (DL-304's
        # re-trigger refusal). Drawn inside the job's own span, so a shrink
        # that drops a job keeps every other draw in place.
        later = [n for n in names[position:] if made.name not in _ancestors(jobs, n)]
        if made.forward is not None and made.forward[0] < len(later):
            offset, template = made.forward
            atom = template.replace("@", later[offset])
            made.condition = atom if made.condition is None else f"{made.condition} | {atom}"
        if made.condition is not None:
            made.lines.append(f"condition: {made.condition}")
    for made in jobs.values():
        members = [n for n, j in jobs.items() if j.parent == made.name]
        if made.box and members:
            if c.chance(30):
                made.lines.append(f"box_success: {_condition(c, members, ())}")
            if c.chance(20):
                made.lines.append(f"box_failure: {_condition(c, members, ())}")
        blocks.append(made.lines)
    jil = "\n\n".join("\n".join(lines) for lines in blocks) + "\n"
    holders = [j.name for j in jobs.values() if j.resources]
    script = steps(c, base, list(jobs), holders, globals_, 4 * size + 8)
    return Case(label=label, jil=jil, script=tuple(script), jobs=len(jobs))


def _job_attributes(
    c: Chooser,
    job: _Job,
    jobs: dict[str, _Job],
    base: datetime,
    resources: dict[str, str],
    globals_: Sequence[str],
    calendars: Sequence[str],
) -> None:
    lines = job.lines
    blocked = _ancestors(jobs, job.name) | {job.name}
    targets = [n for n in jobs if n not in blocked]
    if targets and c.chance(55):
        job.condition = _condition(c, targets, globals_)
    if c.chance(20):
        job.forward = (c.integer(0, 2), _atom(c, "@", globals_))
    if job.parent is not None and c.chance(10):
        lines.append("box_terminator: 1")
    if job.parent is not None and c.chance(10):
        lines.append("auto_hold: 1")
    if job.parent is not None and c.chance(10):
        lines.append("job_terminator: 1")
    if c.chance(12):
        lines.append(f"status: {c.choice(('ON_ICE', 'ON_HOLD', 'ON_NOEXEC'))}")
    if c.chance(30):
        lines.append("date_conditions: 1")
        if calendars and c.chance(30):
            lines.append(f"run_calendar: {c.choice(calendars)}")
        else:
            lines.append(f"days_of_week: {c.choice(('all', 'mo,we,fr', 'sa,su'))}")
        if calendars and c.chance(20):
            lines.append(f"exclude_calendar: {c.choice(calendars)}")
        start = (base.hour * 60 + c.integer(-120, 600)) % 1440
        by_minutes = c.chance(25)
        if by_minutes:
            minutes = sorted({c.integer(0, 59) for _ in range(c.integer(1, 3))})
            lines.append(f"start_mins: {', '.join(map(str, minutes))}")
        else:
            lines.append(f'start_times: "{_clock(start)}"')
        if c.chance(50):
            opens = start + c.integer(-60, 240)
            lines.append(f'run_window: "{_clock(opens)}-{_clock(opens + c.integer(30, 300))}"')
        if c.chance(25):
            lines.append(f"timezone: {c.choice(ZONES)}")
        if c.chance(20):
            lines.append(f"must_start_times: +{c.integer(1, 120)}")
        if not by_minutes and c.chance(15):
            # a time on the next day is written past 24:00 (SEM-34)
            due = start + c.integer(10, 300)
            lines.append(f"must_complete_times: {due // 60:02d}:{due % 60:02d}")
    if not job.box and c.chance(15):
        lines.append(f"term_run_time: {c.integer(1, 120)}")
    if not job.box and resources and c.chance(30):
        refs = []
        for name in sorted({c.choice(sorted(resources)) for _ in range(c.integer(1, 2))}):
            ref = f"({name}, QUANTITY={c.integer(1, 2)}"
            if resources[name] == "R" and c.chance(50):
                ref += f", FREE={c.choice(('Y', 'N', 'A'))}"
            elif resources[name] == "D" and c.chance(30):
                ref += ", FREE=N"
            refs.append(ref + ")")
        lines.append("resources: " + " and ".join(refs))
        job.resources = True
    if not job.box and c.chance(15):
        lines.append(f"job_load: {c.integer(1, 3)}")
        lines.append(f"priority: {c.integer(0, 2)}")
    if not job.box and c.chance(10):
        lines.append(f"max_exit_success: {c.integer(0, 3)}")


def steps(
    c: Chooser,
    base: datetime,
    names: Sequence[str],
    holders: Sequence[str],
    globals_: Sequence[str],
    most: int,
    fewest: int = 0,
) -> list[Step]:
    """`fewest` to `most` inputs over the jobs `names`, from `base` on.
    RELEASE_RESOURCE goes to one of `holders`, the jobs that name a
    resource, when there is one. An input takes at most five draws, the
    most consecutive draws hypothesis deletes in one go."""
    targets = list(holders) or list(names)
    makers: list[Callable[[Chooser, datetime], Step]] = [
        lambda c, t: _status(t, c.choice(names), status=c.choice(STATUSES)),
        lambda c, t: _event(t, "STARTJOB", c.choice(names), source=c.choice((None, "scheduler"))),
        lambda c, t: _status(t, c.choice(names), status=c.choice(STATUSES)),
        lambda c, t: _status(t, c.choice(names), exit_code=c.integer(0, 4)),
        lambda c, t: Advance(t + timedelta(minutes=c.choice(ADVANCE_MINUTES))),
        lambda c, t: _event(t, c.choice(OOB), c.choice(names)),
        lambda c, t: _event(t, "FORCE_STARTJOB", c.choice(names)),
        lambda c, t: _event(t, "KILLJOB", c.choice(names)),
        lambda c, t: _event(t, "RELEASE_RESOURCE", c.choice(targets)),
        lambda c, t: _global(c, t, globals_),
        lambda c, t: SealPoint(),
    ]
    clock = base

    def step(c: Chooser, _index: int) -> Step:
        nonlocal clock
        at = clock + timedelta(minutes=c.choice((0, 1, 3, 10)))
        made = c.choice(makers)(c, at)
        clock = made.at if isinstance(made, Advance) else at
        return made

    return c.items(fewest, most, step, percent=95)


def catalog_script(catalog: CatalogIR, seed: int, length: int) -> list[Step]:
    """A seeded script over an existing catalog, such as a corpus estate."""
    c = RandomChooser(seed)
    names = sorted(catalog.jobs)
    holders = [name for name in names if catalog.jobs[name].resources]
    globals_ = sorted(
        set(catalog.globals_declared)
        | {
            atom.name
            for job in catalog.jobs.values()
            for _kind, cond, _span in job.iter_conditions()
            for atom in iter_atoms(cond)
            if isinstance(atom, GlobalAtom)
        }
    )
    return steps(c, c.choice(BASES), names, holders, globals_, length, length)


def _event(at: datetime, kind: EventKind, job: str, *, source: str | None = None) -> Event:
    return Event(at=at, kind=kind, payload={"job": job}, source=source)


def _status(at: datetime, job: str, **payload: object) -> Event:
    return Event(at=at, kind="STATUS", payload={"job": job, **payload})


def _global(c: Chooser, at: datetime, globals_: Sequence[str]) -> Event:
    name = c.choice(globals_) if globals_ else "G_UNDECLARED"
    return Event(at=at, kind="SET_GLOBAL", payload={"name": name, "value": c.choice(("1", "0"))})


def seeded(seed: int, size: int, min_size: int = 1) -> Case:
    """The case for `seed`: the same on every run."""
    return estate(RandomChooser(seed), size, label=f"seed {seed} size {size}", min_size=min_size)


def cases(max_size: int = 8) -> st.SearchStrategy[Case]:
    """Generated cases with up to `max_size` jobs, shrinking toward small ones."""

    @st.composite
    def _case(draw: st.DrawFn) -> Case:
        return estate(DrawChooser(draw), max_size, label="hypothesis")

    return _case()
