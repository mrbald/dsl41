"""The shared state-machine core: declared transitions, one check, hit recording, and the
statechart executor.

Standard library only. The supervisor tier reaches its helpers by path, as it
reaches `canon.py` (DL-42, DL-72), so this module must not import anything from
this package. tests/test_state_machine.py pins that.

A machine declares its transition table as plain data. The code that moves a
state calls `StateMachine.take(t, old, new)` with the declared transition it is
taking. `take` only checks and records. It never changes what the caller does.

Two environment variables turn on the test tooling. Both are read at call time.

- `DSL41_TRANSITION_HITS=<dir>`: a successful take appends one JSON line to
  `<dir>/hits-<pid>.jsonl` the first time this process sees that
  (machine, id, old, new). A violation appends one line to
  `<dir>/violations-<pid>.jsonl` every time. Each write opens, appends, flushes
  and closes the file, so a process ended with SIGKILL loses nothing. A write
  that fails with OSError is dropped, and a hit is retried on its next take:
  recording never raises out of `take`.
- `DSL41_TRANSITION_STRICT`: a violation raises `TransitionError`.

With neither variable set, `take` returns the violation, or None, and writes
nothing.

The second half of this module is the statechart executor of
docs/statechart-engine.md. A `Statechart` is a declaration in plain data: states
with regions, rows, choices, deferrals and time events, plus a registry of named
guards and actions. An `Executor` runs instances of declarations on one
worklist. It reads no clock and does no I/O; the only file it writes is the
hits file above, and only when `DSL41_TRANSITION_HITS` is set. Machines not yet
converted keep `take`.
"""

from __future__ import annotations

import functools
import itertools
import json
import os
import re
import reprlib
from collections import deque
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Literal, NamedTuple, TypeGuard, cast, overload

HITS_ENV = "DSL41_TRANSITION_HITS"
STRICT_ENV = "DSL41_TRANSITION_STRICT"

type Mark = Literal["spec-only", "unreachable"]


class TransitionError(Exception):
    """A violation, raised only when `DSL41_TRANSITION_STRICT` is set."""


@dataclass(frozen=True)
class Transition[S: str]:
    """One declared transition.

    `target` is a state, or the set of states a payload may choose (a UML
    transition into a choice pseudostate with one branch per member).
    `guard` and `effect` are prose for the generated docs; the code holds the
    predicate.
    `mark` is "spec-only" for a contract rule with no code yet, and
    "unreachable" for a branch the code excludes from coverage.
    """

    id: str
    source: frozenset[S]
    trigger: str
    target: S | frozenset[S]
    guard: str = ""
    effect: str = ""
    cite: str = ""
    mark: Mark | None = None

    def targets(self) -> frozenset[S]:
        """Every state this transition may end in."""
        if isinstance(self.target, frozenset):
            return self.target
        return frozenset({self.target})


@dataclass(frozen=True)
class Violation:
    """A transition that did not match its declaration."""

    machine: str
    transition: str
    old: str
    new: str
    reason: str


#: (directory, machine, id, old, new) already written by this process.
_seen: set[tuple[str, str, str, str, str]] = set()


def state_name(state: str) -> str:
    """The recorded and rendered name of a state: `.value` for an Enum, `str()` otherwise."""
    return str(state.value) if isinstance(state, Enum) else str(state)


def _append(path: str, record: dict[str, str]) -> bool:
    """Append one JSON line and close the file, so nothing is buffered across a kill.

    Returns False when the write failed. Recording must not break the code under test.
    """
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class StateMachine[S: str]:
    """A named machine. `transitions` is the transition table."""

    name: str
    states: frozenset[S]
    initial: S | None
    finals: frozenset[S]
    transitions: tuple[Transition[S], ...]

    def take(self, t: Transition[S], old: S, new: S) -> Violation | None:
        """Check that `old -> new` is the declared transition `t`, and record it.

        Returns the violation, or None. Raises `TransitionError` for a violation
        only when `DSL41_TRANSITION_STRICT` is set. A failed recording write never
        raises, and strict mode still raises for the violation itself.
        """
        violation = self._check(t, old, new)
        directory = os.environ.get(HITS_ENV)
        if violation is None:
            if directory:
                self._record_hit(directory, t, old, new)
            return None
        if directory:
            _append(
                os.path.join(directory, f"violations-{os.getpid()}.jsonl"),
                {
                    "machine": violation.machine,
                    "id": violation.transition,
                    "old": violation.old,
                    "new": violation.new,
                    "reason": violation.reason,
                },
            )
        if os.environ.get(STRICT_ENV):
            raise TransitionError(f"{violation.machine} {violation.transition}: {violation.reason}")
        return violation

    def _check(self, t: Transition[S], old: S, new: S) -> Violation | None:
        reason = ""
        if t not in self.transitions:
            reason = f"transition {t.id} is not declared in machine {self.name}"
        elif old not in t.source:
            reason = f"{state_name(old)} is not a source of {t.id}"
        elif isinstance(t.target, frozenset):
            if new not in t.target:
                reason = f"{state_name(new)} is not one of the targets of {t.id}"
        elif new != t.target:
            reason = f"{t.id} targets {state_name(t.target)}, not {state_name(new)}"
        if not reason:
            return None
        return Violation(self.name, t.id, state_name(old), state_name(new), reason)

    def _record_hit(self, directory: str, t: Transition[S], old: S, new: S) -> None:
        key = (directory, self.name, t.id, state_name(old), state_name(new))
        if key in _seen:
            return
        record = {"machine": self.name, "id": t.id, "old": key[3], "new": key[4]}
        # remembered only after the write lands, so a failed write is retried
        if _append(os.path.join(directory, f"hits-{os.getpid()}.jsonl"), record):
            _seen.add(key)


#: A machine name. Lowercase only, so it is not read as a citation namespace.
NAME_PATTERN = re.compile(r"[a-z][a-z_]*")
#: The digits of a transition id, which is `<machine name>.<digits>`.
ID_DIGITS = re.compile(r"[0-9]{2,3}")


def well_formed[S: str](machine: StateMachine[S] | Statechart) -> list[str]:
    """The problems with a machine's declaration. An empty list means well formed.

    The machine name matches `[a-z][a-z_]*`, and every transition id is
    `<machine name>.<two or three digits>`, such as `host.05`. A
    `Statechart` gets the checks of `chart_problems` instead.
    """
    if is_statechart(machine):
        return chart_problems(machine)
    machine = cast("StateMachine[S]", machine)
    problems = _id_problems(machine.name, [t.id for t in machine.transitions], "transition id")
    if machine.initial is not None and machine.initial not in machine.states:
        problems.append(f"initial state {machine.initial} is not a declared state")
    for state in sorted(machine.finals - machine.states):
        problems.append(f"final state {state} is not a declared state")
    for t in machine.transitions:
        for state in sorted(t.source - machine.states):
            problems.append(f"{t.id}: source {state} is not a declared state")
        if not t.targets():
            problems.append(f"{t.id}: the target set is empty")
        for state in sorted(t.targets() - machine.states):
            problems.append(f"{t.id}: target {state} is not a declared state")
        for state in sorted(t.source & machine.finals):
            problems.append(f"{t.id}: final state {state} has an outgoing transition")
    if machine.initial is not None:
        for state in sorted(machine.states - _reachable(machine, machine.initial)):
            problems.append(f"state {state} is not reachable from {machine.initial}")
    return problems


def _reachable[S: str](machine: StateMachine[S], initial: S) -> set[S]:
    """States reachable from the initial state. A set target reaches every member."""
    seen = {initial}
    frontier = [initial]
    while frontier:
        state = frontier.pop()
        for t in machine.transitions:
            if state in t.source:
                for target in sorted(t.targets() - seen):
                    seen.add(target)
                    frontier.append(target)
    return seen


# ---- the statechart executor (docs/statechart-engine.md)

#: The event of a completion transition (§4). A posted or sent event never has it.
#: A completion item's payload is the name of the completing state.
COMPLETION = ""
#: The event an initial entry's actions see. No declaration may use the name;
#: `well_formed` refuses it, and it is never posted.
INITIAL = "<initial>"
#: The event of the internal row that records an event a full deferred queue
#: dropped under the `drop` policy (§6).
OVERFLOWED = "overflowed"

type RowKind = Literal["external", "internal", "ignored", "refusal"]
type Overflow = Literal["", "stop", "drop"]
type Clock = Literal["", "logical", "monotonic"]
#: One link of an item's ancestry: the instance key and the event name of a
#: microstep that chained the item (§5).
type Step = tuple[Hashable, str]


class Ancestry(Sequence[Step]):
    """A chained item's ancestry, root first (§5): the ancestry of the
    microstep that chained it, shared, and that microstep's own step. A chained
    item extends its parent's in constant time, however deep the chain. It
    reads as the tuple of its steps, and equals that tuple; a read walks the
    links. A root item's ancestry is the empty tuple."""

    __slots__ = ("_parent", "_step", "_depth")

    def __init__(self, parent: Sequence[Step], step: Step) -> None:
        if not isinstance(parent, Ancestry):
            # a tier's own tuple of steps: its links, root first
            link: Ancestry | None = None
            for each in parent:
                link = Ancestry(link or (), each)
            parent = link or ()
        self._parent: Ancestry | None = parent if isinstance(parent, Ancestry) else None
        self._step = step
        self._depth: int = 1 if self._parent is None else len(self._parent) + 1

    def __len__(self) -> int:
        return self._depth

    @property
    def parent(self) -> Ancestry | None:
        """The ancestry this one extends; None for a root's child."""
        return self._parent

    @property
    def step(self) -> Step:
        """The last link: the microstep that chained the item."""
        return self._step

    def steps(self) -> tuple[Step, ...]:
        """The steps, root first."""
        steps: list[Step] = []
        link: Ancestry | None = self
        while link is not None:
            steps.append(link._step)
            link = link._parent
        steps.reverse()
        return tuple(steps)

    def __iter__(self) -> Iterator[Step]:
        return iter(self.steps())

    @overload
    def __getitem__(self, index: int) -> Step: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[Step, ...]: ...

    def __getitem__(self, index: int | slice) -> Step | tuple[Step, ...]:
        return self.steps()[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Ancestry | tuple):
            return self.steps() == tuple(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.steps())

    def __repr__(self) -> str:
        return repr(self.steps())


#: The start of every stderr line of a `StatechartError` that stops a tier, so
#: one journal pattern catches the whole report (deployment runbook, "What to
#: watch").
STATECHART_LOG_PREFIX = "dsl41: statechart error"
#: The exit code of a tier stopped by a `StatechartError`: `dsl41 run`'s and the
#: supervisor's. The units list it in `RestartPreventExitStatus`.
EXIT_STATECHART_ERROR = 7
#: How many recent microsteps an executor keeps per machine for its errors' reports.
HISTORY = 64


class StatechartError(Exception):
    """An assertion failure of a statechart: a bug, never a fault to contain.

    Every tier lets it through to a stop, before any further durable write of
    its own: DL-320's classification re-raises it, and the engine and the
    supervisor exit with `EXIT_STATECHART_ERROR`. An executor fills `facts`
    when the error leaves a run: the machine, the instance, its configuration,
    the item, its ancestry, the candidates, the deferred queue and its
    machine's last microsteps. `str()` renders the message and then one fact
    per line.
    """

    def __init__(self, message: str, candidates: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.candidates = tuple(candidates)
        self.facts: list[tuple[str, str]] = []

    def __str__(self) -> str:
        lines = [self.message]
        lines += [f"{name}: {value}" for name, value in self.facts]
        return "\n".join(lines)

    def report(self) -> str:
        """The whole error, each line under `STATECHART_LOG_PREFIX`, for stderr."""
        return "\n".join(f"{STATECHART_LOG_PREFIX}: {line}" for line in str(self).splitlines())


class AmbiguousTransition(StatechartError):
    """Two enabled rows in one state, or two conflicting candidates of equal depth (§3)."""


class ReentrantDispatch(StatechartError):
    """A call into an executor while one of its macrosteps is in flight (§5)."""


class UnhandledEvent(StatechartError):
    """An event that no region consumes and no active state defers (§10)."""


class DeferralOverflow(StatechartError):
    """A full deferred queue under the `stop` policy (§6)."""


@dataclass(frozen=True)
class Event:
    """An event for one instance. A re-armable time event carries its `generation` (§7)."""

    name: str
    payload: Any = None
    generation: int | None = None


@dataclass(frozen=True)
class Effects:
    """What an action reports (§8).

    `send` lists the events it raises, each for an instance of the same executor.
    `then` names a continuation: an action of the same declaration. It is
    queued behind every item any action of this microstep reported, so it runs
    once their whole cascade is done, and ahead of the microstep's completions. `intents` go back to the tier in the
    outcome. `rearm` names time events whose generation moves on, so that a
    fired event of an older generation is discarded.
    """

    send: tuple[tuple[Hashable, Event], ...] = ()
    then: str = ""
    intents: tuple[Any, ...] = ()
    rearm: tuple[str, ...] = ()


@dataclass(frozen=True)
class Context:
    """What a guard or an action sees: its instance and the item being handled."""

    key: Hashable
    event: Event
    active: frozenset[str]
    data: Any
    ancestry: Sequence[Step] = ()
    #: set by `Executor.resolve`: a read, which records no hit (§5)
    quiet: bool = False
    #: the executor's count of a step in the ancestry (`count`)
    counter: Callable[[Step], int] | None = field(default=None, compare=False, repr=False)

    @property
    def depth(self) -> int:
        """The item's ancestry depth; 0 for a root item (§8)."""
        return len(self.ancestry)

    def count(self, step: Step) -> int:
        """How many links of the item's ancestry are `step` (§8). Under the
        executor it takes constant time, however deep the chain."""
        return self.ancestry.count(step) if self.counter is None else self.counter(step)


type Guard = Callable[[Context], bool]
type Action = Callable[[Context], Effects | None]


@dataclass(frozen=True)
class State:
    """A state; with regions it is composite.

    `entry` and `exit` name actions. `defer` lists the events it defers (§6).
    `wait` lists the replies or time events a wait state awaits; `stays` is the
    reason a wait state may have no way out on them (§9).
    """

    name: str
    regions: tuple[Region, ...] = ()
    entry: str = ""
    exit: str = ""
    defer: frozenset[str] = frozenset()
    final: bool = False
    wait: frozenset[str] = frozenset()
    stays: str = ""


@dataclass(frozen=True)
class Region:
    """An orthogonal region. Its initial pseudostate enters `initial`."""

    name: str
    initial: str
    states: tuple[State, ...]


@dataclass(frozen=True)
class Row:
    """One declared transition of a statechart.

    `kind` is `external` (exit, action, entry), `internal` (the action only),
    `ignored` (consumes the event; the tier writes a trace line) or `refusal`
    (consumes the event and reports `code`). `target` is a state or a choice for
    an external row and empty otherwise. `guard` and `action` name registry
    entries. A row on `COMPLETION` is a completion transition.
    `false_unreachable` is the reason no test can make the guard false: the
    coverage gate then forbids that outcome instead of requiring it. `note` is
    the prose of a row with no guard or action, which has no docstring to
    carry it; the generated table shows it in the action column (§14).
    `mark` is a table transition's mark on a row: "spec-only" for a contract
    rule with no code yet. The coverage gate lists a marked row, never counts
    it, and fails when the row or its guard's true outcome is hit; the guard's
    false outcome is the cell another row takes, neither required nor
    forbidden (§13, DL-353).
    """

    id: str
    source: str
    event: str
    target: str = ""
    guard: str = ""
    action: str = ""
    kind: RowKind = "external"
    code: str = ""
    cite: str = ""
    false_unreachable: str = ""
    note: str = ""
    mark: Mark | None = None


@dataclass(frozen=True)
class Branch:
    """One branch of a choice. An empty `guard` marks the else branch, which comes last."""

    id: str
    target: str
    guard: str = ""
    action: str = ""
    false_unreachable: str = ""


@dataclass(frozen=True)
class Choice:
    """A choice pseudostate: branches tried in order, the else branch last (§4)."""

    name: str
    branches: tuple[Branch, ...]


@dataclass(frozen=True)
class TimeEvent:
    """A declared time event. With `generation` the tier may re-arm it (§7)."""

    name: str
    generation: bool = False


@dataclass(frozen=True)
class Exclusion:
    """A completeness-test cell left out, and why (§13).

    It excludes `event` from every witness in which `state` is active, for one
    payload partition, or for all of them when `partition` is empty.
    """

    state: str
    event: str
    reason: str
    partition: str = ""


@dataclass(frozen=True, eq=False)
class Statechart:
    """A declaration: the type of its instances (§11).

    `root` is the top state. `guards` and `actions` are the registry that rows,
    branches and states name. A chart with a deferral declares `defer_bound`,
    the size of each instance's deferred queue, and its `overflow` policy.
    """

    #: What `is_statechart` reads. The supervisor loads this module by path, so
    #: its charts are instances of another module object's class.
    STATECHART: ClassVar[bool] = True

    name: str
    root: State
    rows: tuple[Row, ...]
    choices: tuple[Choice, ...] = ()
    guards: Mapping[str, Guard] = field(default_factory=dict)
    actions: Mapping[str, Action] = field(default_factory=dict)
    time_events: tuple[TimeEvent, ...] = ()
    clock: Clock = ""
    defer_bound: int = 0
    overflow: Overflow = ""
    exclusions: tuple[Exclusion, ...] = ()


def is_statechart(machine: object) -> TypeGuard[Statechart]:
    """True for a `Statechart` of this module or of its by-path twin; every reader
    dispatches on this, never on `isinstance`."""
    return getattr(type(machine), "STATECHART", False) is True


@dataclass(frozen=True)
class Outcome:
    """The result of one microstep.

    `kind` is `started`, `fired`, `deferred`, `overflowed`, `discarded` or
    `continued`. `rows` lists the rows run, in execution order; `ignored` and
    `refusals` pick out its ignored rows and its refusal codes. `action` names
    the continuation of a `continued` item.
    """

    key: Hashable
    event: Event
    kind: str
    rows: tuple[str, ...] = ()
    branches: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()
    intents: tuple[Any, ...] = ()
    depth: int = 0
    released: bool = False
    action: str = ""


# -- the declaration's tree


@dataclass
class _Index:
    """A declaration's tree and rows, read once."""

    root: str
    states: dict[str, State] = field(default_factory=dict)
    parent: dict[str, str | None] = field(default_factory=dict)
    slot: dict[str, int] = field(default_factory=dict)
    depth: dict[str, int] = field(default_factory=dict)
    order: dict[str, int] = field(default_factory=dict)
    rows: dict[tuple[str, str], list[Row]] = field(default_factory=dict)
    choices: dict[str, Choice] = field(default_factory=dict)
    generational: frozenset[str] = frozenset()
    #: the states of each region, by name: (owning state, region index), where
    #: `(None, 0)` is the region that holds the root
    members: dict[tuple[str | None, int], tuple[str, ...]] = field(default_factory=dict)
    #: what the declaration alone fixes about an external row's run, built on
    #: the row's first run and keyed by the row object's identity (see `_plan`)
    plans: dict[int, _Plan] = field(default_factory=dict)
    #: the initial entry, built on first use
    initial: tuple[str, ...] = ()
    #: the configurations already found legal (`_configuration_problem`), so a
    #: restore into one costs a lookup
    legal: set[frozenset[str]] = field(default_factory=set)


@functools.cache
def _index(chart: Statechart) -> _Index:
    ix = _Index(
        chart.root.name,
        choices={choice.name: choice for choice in chart.choices},
        generational=frozenset(t.name for t in chart.time_events if t.generation),
    )
    _add_state(ix, chart.root, None, 0, 0)
    for row in chart.rows:
        ix.rows.setdefault((row.source, row.event), []).append(row)
    regions: dict[tuple[str | None, int], list[str]] = {}
    for name in sorted(ix.states):
        regions.setdefault((ix.parent[name], ix.slot[name]), []).append(name)
    ix.members = {region: tuple(names) for region, names in regions.items()}
    return ix


def _add_state(ix: _Index, state: State, parent: str | None, slot: int, depth: int) -> None:
    ix.order.setdefault(state.name, len(ix.order))
    ix.states[state.name] = state
    ix.parent[state.name] = parent
    ix.slot[state.name] = slot
    ix.depth[state.name] = depth
    for index, region in enumerate(state.regions):
        for sub in region.states:
            _add_state(ix, sub, state.name, index, depth + 1)


def _all_states(state: State) -> list[State]:
    """`state` and every state below it, in declaration order."""
    found = [state]
    for region in state.regions:
        for sub in region.states:
            found += _all_states(sub)
    return found


def _path(ix: _Index, state: str) -> list[str]:
    """`state` and its ancestors, innermost first."""
    path = [state]
    while (up := ix.parent.get(path[-1])) is not None:
        path.append(up)
    return path


def _below(ix: _Index, state: str, ancestor: str) -> bool:
    """True when `state` is a proper descendant of `ancestor`."""
    return ancestor in _path(ix, state)[1:]


def _split(ix: _Index, a: str, b: str) -> tuple[list[str], list[str], int]:
    """The root-first paths of `a` and `b` and the length of their common prefix."""
    pa, pb = _path(ix, a)[::-1], _path(ix, b)[::-1]
    common = 0
    while common < min(len(pa), len(pb)) and pa[common] == pb[common]:
        common += 1
    return pa, pb, common


def _crosses_regions(ix: _Index, a: str, b: str) -> bool:
    """True when `a` and `b` lie in two sibling regions of their common ancestor (§3)."""
    pa, pb, common = _split(ix, a, b)
    if common == len(pa) or common == len(pb):
        return False
    return ix.slot[pa[common]] != ix.slot[pb[common]]


def _targets(ix: _Index, row: Row) -> tuple[str, ...]:
    """The states an external row may end in: its target, or its choice's branch targets."""
    choice = ix.choices.get(row.target)
    if choice is None:
        return (row.target,)
    return tuple(branch.target for branch in choice.branches)


#: The region a transition runs in: (owning state, region index); `(None, 0)`
#: is the region that holds the root.
type _Domain = tuple[str | None, int]


def _domain(ix: _Index, source: str, targets: Iterable[str]) -> _Domain:
    """The innermost region that holds the source and every target (§4).

    When one endpoint contains the others, that state is left too, so the
    region is the one that holds it. A choice is placed in this region.
    """
    paths = [_path(ix, state)[::-1] for state in (source, *targets)]
    common = 0
    while all(len(p) > common for p in paths) and len({p[common] for p in paths}) == 1:
        common += 1
    top = paths[0][common - 1]
    if any(len(p) == common for p in paths):
        return ix.parent[top], ix.slot[top]
    return top, ix.slot[paths[0][common]]


def _top_below(ix: _Index, source: str, owner: str | None) -> str:
    """The state of `source`'s path that `owner`'s region holds: the root for `None`."""
    path = _path(ix, source)[::-1]
    return path[0] if owner is None else path[path.index(owner) + 1]


def _exit_top(ix: _Index, row: Row) -> str:
    """The outermost state an external row leaves."""
    owner, _ = _domain(ix, row.source, _targets(ix, row))
    return _top_below(ix, row.source, owner)


def _active_child(ix: _Index, active: Iterable[str], owner: str | None, slot: int) -> str:
    """The active state of one region; `owner` None names the root's region."""
    held = active if isinstance(active, (set, frozenset)) else set(active)
    return next(s for s in ix.members.get((owner, slot), ()) if s in held)


def _leaves(ix: _Index, active: Iterable[str]) -> list[str]:
    """The innermost active states, in region declaration order."""
    return sorted((s for s in active if not ix.states[s].regions), key=ix.order.__getitem__)


def _exit_order(ix: _Index, active: frozenset[str], top: str) -> list[str]:
    """The active subtree of `top`, inner to outer, regions in declaration order."""
    order: list[str] = []
    for slot in range(len(ix.states[top].regions)):
        order += _exit_order(ix, active, _active_child(ix, active, top, slot))
    order.append(top)
    return order


def _entry_order(ix: _Index, path: list[str]) -> list[str]:
    """Enter `path[0]`, then down `path`, outer to inner; other regions enter their initial."""
    state = path[0]
    order = [state]
    for slot, region in enumerate(ix.states[state].regions):
        if len(path) > 1 and ix.slot[path[1]] == slot:
            order += _entry_order(ix, path[1:])
        else:
            order += _entry_order(ix, [region.initial])
    return order


def _entry_path(ix: _Index, domain: _Domain, target: str) -> list[str]:
    """The states from just inside `domain` down to `target`."""
    path = _path(ix, target)[::-1]
    owner = domain[0]
    return path if owner is None else path[path.index(owner) + 1 :]


@dataclass
class _Plan:
    """What the declaration alone fixes about one external row's run (§4).

    `top` is the outermost state the row leaves: the one active state of its
    domain's region, since the row's source is active. A `top` without regions
    leaves nothing but itself, so `leaving` is then its whole exit, inner to
    outer; otherwise the exit follows the live configuration. `entries` holds
    the entry order of each target the row has ended in, built on first use.
    """

    row: Row
    domain: _Domain
    top: str
    leaving: tuple[str, ...] | None
    entries: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _build_plan(ix: _Index, row: Row) -> _Plan:
    domain = _domain(ix, row.source, _targets(ix, row))
    top = _top_below(ix, row.source, domain[0])
    return _Plan(row, domain, top, None if ix.states[top].regions else (top,))


def _plan(ix: _Index, row: Row) -> _Plan:
    """The plan of an external row of the chart, built once per chart.

    The key is the row's identity. The plan holds its row, so the row lives
    as long as its plan and the identity cannot be taken by another object.
    """
    plan = ix.plans.get(id(row))
    if plan is None:
        plan = ix.plans[id(row)] = _build_plan(ix, row)
    return plan


def _entry_of(ix: _Index, plan: _Plan, target: str) -> tuple[str, ...]:
    """The states a row enters, outer to inner, when it ends in `target`."""
    order = plan.entries.get(target)
    if order is None:
        order = plan.entries[target] = tuple(_entry_order(ix, _entry_path(ix, plan.domain, target)))
    return order


def _initial_entry(ix: _Index) -> tuple[str, ...]:
    """The states the initial entry enters, outer to inner."""
    if not ix.initial:
        ix.initial = tuple(_entry_order(ix, [ix.root]))
    return ix.initial


# -- hits (test tooling; off in production)

#: (directory, machine, kind, id, detail) already written by this process.
_chart_seen: set[tuple[str, str, str, str, str]] = set()


def _hit(chart: Statechart, kind: str, ident: str, detail: str = "") -> None:
    """Record one coverage item of `chart` (§13), once per process and directory."""
    directory = os.environ.get(HITS_ENV)
    if not directory:
        return
    key = (directory, chart.name, kind, ident, detail)
    if key in _chart_seen:
        return
    record = {"machine": chart.name, "kind": kind, "id": ident, "detail": detail}
    if _append(os.path.join(directory, f"hits-{os.getpid()}.jsonl"), record):
        _chart_seen.add(key)


def coverage_items(
    chart: Statechart,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """The (kind, id, detail) items the gate requires of `chart`, and the ones it forbids.

    Required: every row and branch; both outcomes of every guard; a defer and a
    release for every state and event it defers. Forbidden: a false outcome
    declared unreachable, and a marked row with its guard's true outcome. A
    marked row's false outcome is neither (DL-353).
    """
    required: list[tuple[str, str, str]] = []
    forbidden: list[tuple[str, str, str]] = []
    rows = [r for r in chart.rows if r.mark is None]
    for row in chart.rows:
        if row.mark is not None:
            forbidden.append(("row", row.id, ""))
            if row.guard:
                forbidden.append(("guard", row.id, "true"))
    guarded = [(r.id, r.guard, r.false_unreachable) for r in rows]
    for choice in chart.choices:
        required += [("branch", b.id, "") for b in choice.branches]
        guarded += [(b.id, b.guard, b.false_unreachable) for b in choice.branches]
    required += [("row", r.id, "") for r in rows]
    for ident, guard, unreachable in guarded:
        if guard:
            required.append(("guard", ident, "true"))
            (forbidden if unreachable else required).append(("guard", ident, "false"))
    for state in _all_states(chart.root):
        for event in sorted(state.defer):
            required += [("defer", state.name, event), ("release", state.name, event)]
    return required, forbidden


# -- selection (§3)


def _lookup[T](registry: Mapping[str, T], name: str, what: str) -> T:
    try:
        return registry[name]
    except KeyError:
        raise StatechartError(f"{what} {name!r} is not in the registry") from None


def _holds(chart: Statechart, owner: str, guard: str, ctx: Context) -> bool:
    """Evaluate a row's or a branch's guard; no guard holds."""
    if not guard:
        return True
    held = bool(_lookup(chart.guards, guard, "guard")(ctx))
    if not ctx.quiet:
        _hit(chart, "guard", owner, "true" if held else "false")
    return held


def _enabled(chart: Statechart, ix: _Index, state: str, event: str, ctx: Context) -> Row | None:
    """The enabled row of `state` for `event`, or None. Every guard is evaluated (§3)."""
    rows = [r for r in ix.rows.get((state, event), ()) if _holds(chart, r.id, r.guard, ctx)]
    if len(rows) > 1:
        ids = ", ".join(r.id for r in rows)
        raise AmbiguousTransition(
            f"{chart.name}: rows {ids} are all enabled in {state} for {event or 'completion'}",
            candidates=(r.id for r in rows),
        )
    return rows[0] if rows else None


def _select(
    chart: Statechart, ix: _Index, active: frozenset[str], ctx: Context
) -> tuple[list[Row], list[str]]:
    """The firing set, and the states whose deferral ended a walk. No action runs (§3)."""
    found: dict[str, Row | None] = {}
    candidates: list[Row] = []
    deferring: list[str] = []
    for leaf in _leaves(ix, active):
        state: str | None = leaf
        while state is not None:
            if state not in found:
                found[state] = _enabled(chart, ix, state, ctx.event.name, ctx)
            row = found[state]
            if row is not None:
                if row not in candidates:
                    candidates.append(row)
                break
            if ctx.event.name in ix.states[state].defer:
                if state not in deferring:
                    deferring.append(state)
                break
            state = ix.parent[state]
    # an inner deferral beats the row of a state that regions share
    kept = [r for r in candidates if not any(_below(ix, d, r.source) for d in deferring)]
    firing = _without_conflicts(chart, ix, active, _yielded(chart, kept))
    return _without_exited_sources(ix, active, firing), deferring


def _without_exited_sources(ix: _Index, active: frozenset[str], candidates: list[Row]) -> list[Row]:
    """A candidate whose source another surviving candidate exits is dropped, internal
    rows included (§3): its action would run on a state already left."""
    exits = [(row, _exit_set(ix, active, row)) for row in candidates]
    return [
        row
        for row in candidates
        if not any(row.source in left for other, left in exits if other is not row)
    ]


def _yielded(chart: Statechart, candidates: list[Row]) -> list[Row]:
    """An ignored row yields to any other candidate; a refusal stands alone (§3)."""
    consuming = [row for row in candidates if row.kind != "ignored"]
    if consuming:
        candidates = consuming
    refusals = [row.id for row in candidates if row.kind == "refusal"]
    if refusals and len(candidates) > 1:
        others = ", ".join(row.id for row in candidates if row.id != refusals[0])
        raise AmbiguousTransition(
            f"{chart.name}: refusal {refusals[0]} beside {others}",
            candidates=(row.id for row in candidates),
        )
    return candidates


def _exit_set(ix: _Index, active: frozenset[str], row: Row) -> frozenset[str]:
    if row.kind != "external":
        return frozenset()
    plan = _plan(ix, row)
    return frozenset(plan.leaving or _exit_order(ix, active, plan.top))


def _without_conflicts(
    chart: Statechart, ix: _Index, active: frozenset[str], candidates: list[Row]
) -> list[Row]:
    """Resolve crossing exit sets from the deepest candidate outward (§3).

    A candidate whose exit set meets a kept deeper one is dropped first; two
    that remain at one depth and meet raise. The kept rows keep region order.
    """
    exits = {row.id: _exit_set(ix, active, row) for row in candidates}
    kept: list[Row] = []
    for depth in sorted({ix.depth[row.source] for row in candidates}, reverse=True):
        level = [
            row
            for row in candidates
            if ix.depth[row.source] == depth and not any(exits[row.id] & exits[k.id] for k in kept)
        ]
        for a, b in itertools.combinations(level, 2):
            if exits[a.id] & exits[b.id]:
                raise AmbiguousTransition(
                    f"{chart.name}: {a.id} and {b.id} leave the same states from sources"
                    " of equal depth",
                    candidates=(row.id for row in candidates),
                )
        kept += level
    return [row for row in candidates if row in kept]


# -- the executor (§4-§8)


#: The empty mapping a new record starts with.
_NOTHING: Mapping[str, int] = MappingProxyType({})


@dataclass(frozen=True)
class _Deferred:
    event: Event
    states: tuple[str, ...]


class _Instance(NamedTuple):
    """One instance's record. A stored record is never changed: a write stores a
    new one (`Executor._write`), so a fork shares the records until it writes
    (DL-339). The mappings are never written in place either. A named tuple,
    because its `_replace` is the cheapest copy with changes."""

    chart: Statechart
    data: Any
    active: frozenset[str]
    deferred: tuple[_Deferred, ...] = ()
    generations: Mapping[str, int] = _NOTHING
    #: each state's latest entry, by a serial of this instance's entries: a
    #: completion belongs to one occurrence of its state (§4). A state active
    #: since `restore` has no entry yet and reads as serial 0
    entered: Mapping[str, int] = _NOTHING
    entries: int = 0
    #: the executor's serial of this instance's addition: the release scan
    #: visits deferring instances in this order (§6)
    added: int = 0


@dataclass(frozen=True)
class _Item:
    """One worklist item (§5). `name` is the completing state or the continuation."""

    key: Hashable
    kind: Literal["event", "completion", "continuation", "start"]
    event: Event
    ancestry: Sequence[Step] = ()
    name: str = ""
    released: bool = False
    #: a completion's entry serial of its state
    serial: int = 0


@dataclass
class _Microstep:
    """What one microstep gathers while it runs."""

    item: _Item
    reported: list[_Item] = field(default_factory=list)
    continuations: list[_Item] = field(default_factory=list)
    entered: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    intents: list[Any] = field(default_factory=list)
    _chained: Ancestry | None = None

    def chained(self) -> Ancestry:
        """The ancestry of the items this microstep produces, one link on the
        item's, built once and shared by all of them."""
        if self._chained is None:
            self._chained = Ancestry(self.item.ancestry, (self.item.key, self.item.event.name))
        return self._chained


#: The event of an initial entry (`INITIAL`).
START = Event(INITIAL)


def _check_event(chart: Statechart, event: Event) -> None:
    if event.name in (COMPLETION, OVERFLOWED, INITIAL):
        raise StatechartError(f"{chart.name}: {event.name or 'completion'} is not an event to post")
    if event.name in _index(chart).generational and event.generation is None:
        raise StatechartError(f"{chart.name}: time event {event.name} needs its generation")


def _configuration_problem(ix: _Index, active: frozenset[str]) -> str:
    """Why `active` is not a configuration of the chart; empty when it is. A
    legal one is remembered, and found again by a lookup."""
    if active in ix.legal:
        return ""
    states = set(active)
    unknown = sorted(states - set(ix.states))
    if unknown:
        return f"{unknown[0]} is not a declared state"
    if ix.root not in states:
        return f"the root state {ix.root} is not active"
    for state in sorted(states):
        parent = ix.parent[state]
        if parent is not None and parent not in states:
            return f"{state} is active and its parent {parent} is not"
        for slot, region in enumerate(ix.states[state].regions):
            count = sum(1 for s in states if ix.parent[s] == state and ix.slot[s] == slot)
            if count != 1:
                return f"{state} has {count} active states in region {region.name}"
    ix.legal.add(active)
    return ""


def _completions(ix: _Index, inst: _Instance, step: _Microstep) -> list[_Item]:
    """The completion items of what this microstep entered, in entry order (§4).

    A simple state completes when entered; a composite completes when every
    region rests in a final state. Only a state with a completion row queues one.
    Each item carries its state's entry serial, so it is discarded if the state
    was left and entered again before it pops.
    """
    states: list[str] = []
    for state in step.entered:
        node = ix.states[state]
        if node.final:
            # well_formed refuses a final root, so a final state has a parent
            state = ix.parent[state] or ix.root
            if not all(ix.states[s].final for s in inst.active if ix.parent[s] == state):
                continue
        elif node.regions:
            continue
        if (state, COMPLETION) in ix.rows and state not in states:
            states.append(state)
    return [
        _Item(
            step.item.key,
            "completion",
            Event(COMPLETION, state),
            step.chained(),
            state,
            serial=inst.entered.get(state, 0),
        )
        for state in states
    ]


def _occurrence_gone(inst: _Instance, item: _Item) -> bool:
    """A completion's state was left, or left and entered again, since the
    completion was queued: a completion belongs to one occurrence of its state (§4)."""
    return item.name not in inst.active or inst.entered.get(item.name, 0) != item.serial


def _queue_order(step: _Microstep, completions: list[_Item]) -> list[_Item]:
    """What a microstep puts at the front of the worklist (§5 step 3): the items its
    actions reported, then its continuations, then its completions."""
    return [*step.reported, *step.continuations, *completions]


class Executor:
    """One executor per tier object: one worklist and one pool for all its instances.

    `post` puts a root item in the pool and `run` drains everything, one
    macrostep per root item, released deferred events ahead of the pool (§5,
    §6). `dispatch` does both, and both take the tier's hook, called with
    each outcome before the next item pops. `fork` copies an idle executor,
    sharing its instance records until either side writes one (DL-339). A
    call while `run` is in flight raises `ReentrantDispatch`. An exception out
    of a guard, an action or the hook ends the run and empties the worklist; the instance stays as
    the failing microstep left it. Its configuration stays readable, and the
    tier dispatches nothing more to that instance.
    """

    def __init__(self, history: int = HISTORY) -> None:
        self._instances: dict[Hashable, _Instance] = {}
        self._pool: deque[_Item] = deque()
        self._worklist: deque[_Item] = deque()
        self._running = False
        #: the item in hand, for an error's report
        self._current: _Item | None = None
        #: each machine's last `history` microsteps, one line each, for an
        #: error's report: another machine's steps never evict them
        self._history = history
        self._recent: dict[str, deque[str]] = {}
        #: the instances with a non-empty deferred queue, the only ones the
        #: release scan visits; and the serial the next added instance takes
        self._deferring: set[Hashable] = set()
        self._added = 0
        #: the ancestry of the item in hand, root first, and how many of its
        #: links each step is: the worklist is depth first, so the next item's
        #: parent is on this path, and keeping it costs constant time per item
        self._path: list[Ancestry] = []
        self._on_path: dict[Step, int] = {}

    # -- the tier's calls

    def start(self, key: Hashable, chart: Statechart, data: Any = None) -> None:
        """Add an instance. Its initial entry runs as a root item of the next run."""
        self._add(key, _Instance(chart, data, frozenset()))
        self._pool.append(_Item(key, "start", START))

    def restore(
        self,
        key: Hashable,
        chart: Statechart,
        active: Iterable[str],
        data: Any = None,
        generations: Mapping[str, int] | None = None,
    ) -> None:
        """Add an instance in a given configuration; no action runs."""
        active = frozenset(active)
        problem = _configuration_problem(_index(chart), active)
        if problem:
            raise StatechartError(f"{chart.name} {key!r}: {problem}")
        self._add(key, _Instance(chart, data, active, generations=dict(generations or {})))

    def forget(self, key: Hashable) -> None:
        """Remove an instance that rests in a final configuration: every region of
        its root in a final state. Any other instance, one with a deferred event,
        or one with an item still in the pool raises, so no event is lost."""
        self._idle()
        inst = self._instance(key)
        ix = _index(inst.chart)
        if ix.states[ix.root].regions == () or not all(
            ix.states[s].final for s in inst.active if ix.parent[s] == ix.root
        ):
            raise StatechartError(f"{inst.chart.name} {key!r}: not in a final configuration")
        if inst.deferred or any(item.key == key for item in self._pool):
            raise StatechartError(f"{inst.chart.name} {key!r}: has events still to handle")
        del self._instances[key]

    def post(self, key: Hashable, event: Event) -> None:
        """Put an external event in the pool."""
        self._idle()
        _check_event(self._instance(key).chart, event)
        self._pool.append(_Item(key, "event", event))

    def dispatch(
        self, key: Hashable, event: Event, after: Callable[[Outcome], None] | None = None
    ) -> list[Outcome]:
        """Post one event and run, with `run`'s hook."""
        self.post(key, event)
        return self.run(after)

    def run(self, after: Callable[[Outcome], None] | None = None) -> list[Outcome]:
        """Run every item to completion and return each microstep's outcome.

        `after` is the tier's hook (§5 step 5): it is called with each
        outcome once the microstep has recorded it, before the next item pops,
        under the macrostep. It may read an instance; a post, dispatch,
        resolve or fork from it raises `ReentrantDispatch`, and it reports no
        item. An exception out of it ends the run as one out of an
        action does, its facts naming the item."""
        self._idle()
        self._running = True
        outcomes: list[Outcome] = []
        try:
            while True:
                while self._worklist:
                    self._current = self._worklist.popleft()
                    self._align(self._current.ancestry)
                    outcome = self._step(self._current)
                    outcomes.append(outcome)
                    name = self._instances[outcome.key].chart.name
                    ring = self._recent.setdefault(name, deque(maxlen=self._history))
                    ring.append(_summary(outcome))
                    if after is not None:
                        after(outcome)
                self._current = None
                item = self._release() or (self._pool.popleft() if self._pool else None)
                if item is None:
                    return outcomes
                self._worklist.append(item)
        except BaseException as error:
            if isinstance(error, StatechartError) and not error.facts:
                # an error leaves a run only from a step or a release, and both set it
                assert self._current is not None
                error.facts = self._facts(error, self._current)
            self._worklist.clear()
            raise
        finally:
            self._running = False
            self._current = None
            self._path.clear()
            self._on_path.clear()

    def resolve(self, key: Hashable, event: Event) -> Outcome:
        """Read how `event` would be selected for `key` as it stands, and do none of it.

        The selection of §3 runs for this instance and event alone, against the
        instance's present configuration: guards are evaluated, no action runs, the
        configuration, the deferred queue and the history ring stay as they are, and
        no hit is recorded. A guard that reaches into the executor meets
        `ReentrantDispatch`, as under a dispatch. The outcome is the `kind`
        (`fired`, `deferred` or `discarded`) with its `rows`, `ignored` and
        `refusals`; it names no `branches` (a choice resolves as the row runs). A
        selection error (unhandled, ambiguous) is raised with a dispatch's facts.

        The read is not a forecast of a dispatch. A dispatch first releases deferred
        events and runs the pool ahead of the event, and a full deferred queue meets
        its overflow policy; the read answers none of that. An instance with a
        deferred event is therefore refused, and the pool is outside the read.
        """
        self._idle()
        inst = self._instance(key)
        _check_event(inst.chart, event)
        item = _Item(key, "event", event)
        self._running = True
        try:
            if inst.deferred:
                raise StatechartError(
                    f"{inst.chart.name} {key!r}: a read is exact only with nothing deferred;"
                    f" {len(inst.deferred)} deferred"
                )
            kind, firing, _ = self._decide(inst, item, self._context(inst, item, quiet=True))
        except StatechartError as error:
            if not error.facts:
                error.facts = self._facts(error, item)
            raise
        finally:
            self._running = False
        return Outcome(
            key,
            event,
            kind,
            rows=tuple(r.id for r in firing),
            ignored=tuple(r.id for r in firing if r.kind == "ignored"),
            refusals=tuple(r.code for r in firing if r.kind == "refusal"),
        )

    def fork(self, data: Mapping[Hashable, Any] | None = None) -> Executor:
        """A copy of this idle executor that shares its instance records (DL-339).

        The copy holds every instance, each in its configuration with its
        deferred queue, generations and entry serials, and the history rings as
        they stand. A record is shared until a dispatch on either side writes
        it, and a write stores a new record on that side only, so neither side
        sees the other's moves. `data` gives the copy new data for the instances
        it names; every other instance shares its data object with this one.
        Event payloads and data that get no new value stay shared by design, so
        a tier treats them as immutable.

        A fork inside a macrostep raises `ReentrantDispatch`; a fork with a root
        item still in the pool raises a `StatechartError`, because the item
        would run on one side only.
        """
        if self._running:
            raise ReentrantDispatch("a fork while a macrostep of this executor is in flight")
        if self._pool:
            raise StatechartError(f"a fork with {len(self._pool)} root items still in the pool")
        twin = Executor(self._history)
        twin._instances = dict(self._instances)
        twin._recent = {name: ring.copy() for name, ring in self._recent.items()}
        twin._deferring = set(self._deferring)
        twin._added = self._added
        for key, value in (data or {}).items():
            twin._instance(key)
            twin._write(key, data=value)
        return twin

    # -- reading an instance

    def active(self, key: Hashable) -> frozenset[str]:
        """The instance's active states."""
        return self._instance(key).active

    def deferred(self, key: Hashable) -> tuple[Event, ...]:
        """The instance's deferred queue, head first."""
        return tuple(d.event for d in self._instance(key).deferred)

    def generation(self, key: Hashable, name: str) -> int:
        """The current generation of a re-armable time event; 0 before the first re-arm."""
        return self._instance(key).generations.get(name, 0)

    def data(self, key: Hashable) -> Any:
        """The data the instance was added with."""
        return self._instance(key).data

    def explain(self, error: StatechartError, key: Hashable, event: Event) -> StatechartError:
        """Give an error the tier raises about an instance's last outcome the
        facts an error out of a run carries (§5): the machine, the instance,
        its configuration, `event` as the item, the candidates the error
        names, the deferred queue and the machine's recent microsteps. An
        unknown instance raises; facts the error already carries stay."""
        self._idle()
        self._instance(key)
        if not error.facts:
            error.facts = self._facts(error, _Item(key, "event", event))
        return error

    # -- inside

    def _idle(self) -> None:
        if self._running:
            raise ReentrantDispatch("a dispatch while a macrostep of this executor is in flight")

    def _add(self, key: Hashable, instance: _Instance) -> None:
        self._idle()
        if key in self._instances:
            raise StatechartError(f"instance {key!r} already exists")
        self._added += 1
        self._instances[key] = instance._replace(added=self._added)

    def _write(self, key: Hashable, **changes: Any) -> _Instance:
        """The one write of an instance: store a new record with `changes`. The
        record it replaces may be shared with a fork, so it stays as it was
        (DL-339)."""
        record = self._instances[key]._replace(**changes)
        self._instances[key] = record
        return record

    def _instance(self, key: Hashable) -> _Instance:
        try:
            return self._instances[key]
        except KeyError:
            raise StatechartError(f"no instance {key!r}") from None

    def _facts(self, error: StatechartError, item: _Item) -> list[tuple[str, str]]:
        """What an error's report says: the item in hand and the executor's recent past."""
        inst = self._instances[item.key]
        ancestry = " > ".join(f"{key!r} {name or 'completion'}" for key, name in item.ancestry)
        queue = ", ".join(f"{d.event.name} {reprlib.repr(d.event.payload)}" for d in inst.deferred)
        facts = [
            ("machine", inst.chart.name),
            ("instance", repr(item.key)),
            ("active", ", ".join(sorted(inst.active)) or "nothing yet"),
            ("item", _describe(item)),
            ("payload", reprlib.repr(item.event.payload)),
            ("ancestry", ancestry or "none: a root item"),
            ("deferred", queue or "empty"),
        ]
        facts.append(("candidates", ", ".join(error.candidates) or "none"))
        recent = self._recent.get(inst.chart.name, ())
        facts.append(("recent microsteps", f"{len(recent)}, oldest first"))
        facts += [("microstep", line) for line in recent]
        return facts

    def _context(self, inst: _Instance, item: _Item, quiet: bool = False) -> Context:
        counter = functools.partial(self._count, item.ancestry)
        return Context(item.key, item.event, inst.active, inst.data, item.ancestry, quiet, counter)

    def _align(self, ancestry: Sequence[Step]) -> None:
        """Make the path the item's ancestry: drop the links past the part it
        shares, then add its own."""
        path, counts = self._path, self._on_path
        missing: list[Ancestry] = []
        link = ancestry if isinstance(ancestry, Ancestry) else None
        while link is not None and not (len(path) >= len(link) and path[len(link) - 1] is link):
            missing.append(link)
            link = link.parent
        keep = 0 if link is None else len(link)
        while len(path) > keep:
            step = path.pop().step
            if counts[step] == 1:
                del counts[step]
            else:
                counts[step] -= 1
        for link in reversed(missing):
            path.append(link)
            counts[link.step] = counts.get(link.step, 0) + 1

    def _count(self, ancestry: Sequence[Step], step: Step) -> int:
        """`step`'s links in `ancestry`: read off the path when the ancestry is
        the item in hand's, else counted link by link."""
        top = self._path[-1] if self._path else ()
        if top is ancestry or (not self._path and not ancestry):
            return self._on_path.get(step, 0)
        return sum(1 for link in ancestry if link == step)

    @staticmethod
    def _stale(inst: _Instance, event: Event) -> bool:
        """A re-armable time event of an older generation (§7)."""
        return event.name in _index(inst.chart).generational and event.generation != (
            inst.generations.get(event.name, 0)
        )

    def _step(self, item: _Item) -> Outcome:
        """One microstep: select and execute (§5 steps 1-3)."""
        inst = self._instances[item.key]
        chart, ix = inst.chart, _index(inst.chart)
        if item.kind == "continuation":
            return self._fire(item, [], "continued")
        if item.kind == "start":
            return self._fire(item, [], "started")
        ctx = self._context(inst, item)
        if item.kind == "completion":
            if _occurrence_gone(inst, item):
                return _outcome(item, "discarded")
            row = _enabled(chart, ix, item.name, COMPLETION, ctx)
            if row is None:
                raise UnhandledEvent(f"{chart.name} {item.key!r}: {item.name} completed")
            return self._fire(item, [row], "fired")
        kind, firing, deferring = self._decide(inst, item, ctx)
        if kind == "discarded":
            return _outcome(item, kind)
        if kind == "fired":
            return self._fire(item, firing, kind)
        return self._defer(inst, item, deferring, ctx)

    def _decide(
        self, inst: _Instance, item: _Item, ctx: Context
    ) -> tuple[str, list[Row], list[str]]:
        """What an event item comes to: `discarded`, `fired` with its firing set, or
        `deferred` with the states that defer it; or an unhandled event raises. It
        reads and changes nothing (§3, §5)."""
        if self._stale(inst, item.event):
            return "discarded", [], []
        firing, deferring = _select(inst.chart, _index(inst.chart), inst.active, ctx)
        if firing:
            return "fired", firing, deferring
        if deferring:
            return "deferred", [], deferring
        raise UnhandledEvent(
            f"{inst.chart.name} {item.key!r}: no row and no deferral for {item.event.name}"
            f" in {sorted(inst.active)}"
        )

    def _defer(self, inst: _Instance, item: _Item, deferring: list[str], ctx: Context) -> Outcome:
        """Queue the event, or apply the overflow policy (§6)."""
        chart = inst.chart
        if len(inst.deferred) < chart.defer_bound:
            self._write(
                item.key, deferred=(*inst.deferred, _Deferred(item.event, tuple(deferring)))
            )
            self._deferring.add(item.key)
            for state in deferring:
                _hit(chart, "defer", state, item.event.name)
            return _outcome(item, "deferred")
        if chart.overflow != "drop":
            raise DeferralOverflow(
                f"{chart.name} {item.key!r}: the deferred queue is full"
                f" ({chart.defer_bound}) for {item.event.name}"
            )
        row = _enabled(chart, _index(chart), deferring[0], OVERFLOWED, ctx)
        if row is None:
            raise UnhandledEvent(f"{chart.name}: no enabled overflowed row in {deferring[0]}")
        return self._fire(item, [row], "overflowed")

    def _release(self) -> _Item | None:
        """The first deferred head its configuration no longer defers, as a root item (§6).

        A head deferred again holds its place, and its queue waits for a later
        macrostep.
        """
        for key in sorted(self._deferring, key=lambda k: self._instances[k].added):
            inst = self._instances[key]
            head = inst.deferred[0]
            item = self._current = _Item(key, "event", head.event, released=True)
            if not self._stale(inst, head.event):
                ix = _index(inst.chart)
                firing, deferring = _select(inst.chart, ix, inst.active, self._context(inst, item))
                if not firing and deferring:
                    continue
            if not self._write(key, deferred=inst.deferred[1:]).deferred:
                self._deferring.discard(key)
            for state in head.states:
                _hit(inst.chart, "release", state, head.event.name)
            return item
        return None

    def _fire(self, item: _Item, rows: list[Row], kind: str) -> Outcome:
        """Execute a firing set in region order and queue what it reported (§4, §5 step 3).

        Each write stores a new record (`_write`), so every read of the instance
        here and below takes the record in `_instances` as it stands."""
        ix = _index(self._instances[item.key].chart)
        step = _Microstep(item)
        if kind == "continued":
            self._call(step, item.name)
        if kind == "started":
            self._enter(step, _initial_entry(ix))
        for row in rows:
            self._run_row(step, row)
        queued = _queue_order(step, _completions(ix, self._instances[item.key], step))
        self._worklist.extendleft(reversed(queued))
        return Outcome(
            item.key,
            item.event,
            kind,
            rows=tuple(r.id for r in rows),
            branches=tuple(step.branches),
            ignored=tuple(r.id for r in rows if r.kind == "ignored"),
            refusals=tuple(r.code for r in rows if r.kind == "refusal"),
            intents=tuple(step.intents),
            depth=len(item.ancestry),
            released=item.released,
            action=item.name if kind == "continued" else "",
        )

    def _run_row(self, step: _Microstep, row: Row) -> None:
        key = step.item.key
        chart = self._instances[key].chart
        ix = _index(chart)
        _hit(chart, "row", row.id)
        if row.kind != "external":
            self._call(step, row.action)
            return
        plan = _plan(ix, row)
        for state in plan.leaving or _exit_order(ix, self._instances[key].active, plan.top):
            self._call(step, ix.states[state].exit)
            self._write(key, active=self._instances[key].active - {state})
        self._call(step, row.action)
        target = row.target
        choice = ix.choices.get(target)
        if choice is not None:
            target = self._choose(step, choice)
        self._enter(step, _entry_of(ix, plan, target))

    def _choose(self, step: _Microstep, choice: Choice) -> str:
        """Resolve a choice inside the microstep: the first enabled branch (§4 step 3)."""
        inst = self._instances[step.item.key]
        chart = inst.chart
        ctx = self._context(inst, step.item)
        branch = next((b for b in choice.branches if _holds(chart, b.id, b.guard, ctx)), None)
        if branch is None:
            raise UnhandledEvent(f"{chart.name}: choice {choice.name} has no enabled branch")
        _hit(chart, "branch", branch.id)
        step.branches.append(branch.id)
        self._call(step, branch.action)
        return branch.target

    def _enter(self, step: _Microstep, order: Sequence[str]) -> None:
        key = step.item.key
        ix = _index(self._instances[key].chart)
        for state in order:
            inst = self._instances[key]
            entries = inst.entries + 1
            self._write(
                key,
                active=inst.active | {state},
                entries=entries,
                entered={**inst.entered, state: entries},
            )
            self._call(step, ix.states[state].entry)
            step.entered.append(state)

    def _call(self, step: _Microstep, name: str) -> None:
        """Run one action and gather what it reports (§8)."""
        if not name:
            return
        item = step.item
        inst = self._instances[item.key]
        effects = _lookup(inst.chart.actions, name, "action")(self._context(inst, item))
        if effects is None:
            return
        for key, event in effects.send:
            _check_event(self._instance(key).chart, event)
            step.reported.append(_Item(key, "event", event, step.chained()))
        if effects.then:
            step.continuations.append(
                _Item(item.key, "continuation", item.event, step.chained(), effects.then)
            )
        step.intents.extend(effects.intents)
        for timer in effects.rearm:
            if timer not in _index(inst.chart).generational:
                raise StatechartError(f"{inst.chart.name}: {timer} is not a re-armable time event")
            generations = self._instances[item.key].generations
            self._write(item.key, generations={**generations, timer: generations.get(timer, 0) + 1})


def _describe(item: _Item) -> str:
    """An item as an error's report names it."""
    event = item.event.name or "completion"
    if item.kind == "completion":
        return f"the completion of {item.name}"
    if item.kind == "continuation":
        return f"the continuation {item.name} of {event}"
    if item.kind == "start":
        return "the initial entry"
    return f"the released event {event}" if item.released else f"the event {event}"


def _summary(outcome: Outcome) -> str:
    """One microstep as a line of an error's report."""
    rows = f" rows {', '.join(outcome.rows)}" if outcome.rows else ""
    event = outcome.event.name or "completion"
    return f"{outcome.key!r} {event} {outcome.kind}{rows} depth {outcome.depth}"


def _outcome(item: _Item, kind: str) -> Outcome:
    return Outcome(item.key, item.event, kind, depth=len(item.ancestry), released=item.released)


# -- well-formedness (§3, §6, §9, §10)


def alphabet(chart: Statechart) -> list[str]:
    """The chart's events: those of its rows, deferrals, waits and time events, sorted."""
    events = {row.event for row in chart.rows} | {t.name for t in chart.time_events}
    for state in _all_states(chart.root):
        events |= state.defer | state.wait
    return sorted(events - {COMPLETION, OVERFLOWED})


def chart_problems(chart: Statechart) -> list[str]:
    """The problems with a statechart's declaration. An empty list means well formed.

    Names, ids, the tree and the registry come first; the checks that walk the
    tree (sibling regions, reachability, completeness) run only on a sound one.
    """
    ix = _index(chart)
    ids = [r.id for r in chart.rows] + [b.id for c in chart.choices for b in c.branches]
    problems = _id_problems(chart.name, ids, "id")
    if INITIAL in alphabet(chart):
        problems.append(f"the event name {INITIAL} is reserved for the initial entry")
    problems += _tree_problems(chart)
    problems += _row_problems(chart, ix)
    problems += _choice_problems(chart, ix)
    if problems:
        return problems
    problems += _sibling_problems(chart, ix)
    problems += _defer_problems(chart, ix)
    problems += _wait_problems(chart, ix)
    problems += _reach_problems(chart, ix)
    for event in alphabet(chart):
        hole = _fall_through(ix, ix.root, event)
        if hole is not None:
            problems.append(f"event {event} falls through in configuration {{{', '.join(hole)}}}")
    for exclusion in chart.exclusions:
        if not exclusion.reason or exclusion.state not in ix.states:
            problems.append(f"exclusion {exclusion}: needs a declared state and a reason")
    return problems


def _id_problems(name: str, ids: list[str], what: str) -> list[str]:
    """The machine name and the shape and uniqueness of its ids."""
    problems: list[str] = []
    if not NAME_PATTERN.fullmatch(name):
        problems.append(f"machine name {name!r} does not match [a-z][a-z_]*")
    prefix = name + "."
    for ident in ids:
        if not (ident.startswith(prefix) and ID_DIGITS.fullmatch(ident[len(prefix) :])):
            problems.append(f"{what} {ident!r} is not {name}.<two or three digits>")
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"duplicate {what} {dup}")
    return problems


def _tree_problems(chart: Statechart) -> list[str]:
    problems: list[str] = []
    states = _all_states(chart.root)
    names = [s.name for s in states] + [c.name for c in chart.choices]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"duplicate state or choice name {dup}")
    if chart.root.final:
        problems.append(f"the root state {chart.root.name} is final")
    if chart.clock not in ("logical", "monotonic"):
        problems.append("the chart declares no clock (logical or monotonic)")
    for state in states:
        for region in state.regions:
            if region.initial not in {s.name for s in region.states}:
                problems.append(
                    f"region {state.name}/{region.name}: initial {region.initial!r}"
                    " is not one of its states"
                )
        if state.final and (state.regions or state.defer or state.wait):
            problems.append(f"final state {state.name} has regions, deferrals or waits")
        for action in (state.entry, state.exit):
            if action and action not in chart.actions:
                problems.append(f"state {state.name}: action {action} is not in the registry")
    return problems


def _registry_problems(chart: Statechart, where: str, guard: str, action: str) -> list[str]:
    problems: list[str] = []
    if guard and guard not in chart.guards:
        problems.append(f"{where}: guard {guard} is not in the registry")
    if action and action not in chart.actions:
        problems.append(f"{where}: action {action} is not in the registry")
    return problems


def _row_problems(chart: Statechart, ix: _Index) -> list[str]:
    problems: list[str] = []
    for row in chart.rows:
        problems += _registry_problems(chart, row.id, row.guard, row.action)
        if row.source not in ix.states:
            problems.append(f"{row.id}: source {row.source} is not a declared state")
        elif ix.states[row.source].final:
            problems.append(f"{row.id}: final state {row.source} has a row")
        if row.kind == "external" and row.target not in {*ix.states, *ix.choices}:
            problems.append(f"{row.id}: target {row.target!r} is not a declared state or choice")
        if row.kind != "external" and row.target:
            problems.append(f"{row.id}: an {row.kind} row has no target")
        if row.kind in ("ignored", "refusal") and row.action:
            problems.append(f"{row.id}: an {row.kind} row runs no action")
        if (row.kind == "refusal") != bool(row.code):
            problems.append(f"{row.id}: a refusal row, and only a refusal row, has a code")
        if row.event == COMPLETION and row.kind != "external":
            problems.append(f"{row.id}: a completion row is external")
        if row.event == OVERFLOWED and row.kind != "internal":
            problems.append(f"{row.id}: an overflowed row is internal")
        if row.false_unreachable and not row.guard:
            problems.append(f"{row.id}: false_unreachable without a guard")
    return problems


def _choice_problems(chart: Statechart, ix: _Index) -> list[str]:
    problems: list[str] = []
    for choice in chart.choices:
        guards = [b.guard for b in choice.branches]
        if "" not in guards:
            problems.append(f"choice {choice.name} has no else branch")
        elif guards.index("") != len(guards) - 1:
            problems.append(f"choice {choice.name}: the else branch is not the only last one")
        for branch in choice.branches:
            problems += _registry_problems(chart, branch.id, branch.guard, branch.action)
            if branch.target not in ix.states:
                problems.append(f"{branch.id}: target {branch.target!r} is not a declared state")
            if branch.false_unreachable and not branch.guard:
                problems.append(f"{branch.id}: false_unreachable without a guard")
        if not any(row.target == choice.name for row in chart.rows):
            problems.append(f"choice {choice.name} is the target of no row")
    return problems


def _sibling_problems(chart: Statechart, ix: _Index) -> list[str]:
    """A row never targets a state in a sibling region (§3)."""
    return [
        f"{row.id}: target {target} is in a sibling region of {row.source}"
        for row in chart.rows
        if row.kind == "external"
        for target in _targets(ix, row)
        if _crosses_regions(ix, row.source, target)
    ]


def _defer_problems(chart: Statechart, ix: _Index) -> list[str]:
    problems: list[str] = []
    deferring = [s.name for s in _all_states(chart.root) if s.defer]
    if deferring and (chart.defer_bound < 1 or chart.overflow not in ("stop", "drop")):
        problems.append("a state defers, so the chart declares defer_bound >= 1 and stop or drop")
    if chart.overflow == "drop":
        for state in deferring:
            if (state, OVERFLOWED) not in ix.rows:
                problems.append(f"{state} defers under drop and declares no overflowed row")
    for row in chart.rows:
        if row.event == OVERFLOWED and not ix.states[row.source].defer:
            problems.append(f"{row.id}: an overflowed row on {row.source}, which defers nothing")
    return problems


def _wait_problems(chart: Statechart, ix: _Index) -> list[str]:
    """A wait state has a way out on what it awaits, or a reason to stay (§9)."""
    problems: list[str] = []
    for state in _all_states(chart.root):
        if not state.wait or state.stays:
            continue
        rows = [r for e in sorted(state.wait) for r in ix.rows.get((state.name, e), ())]
        if not any(r.kind == "external" for r in rows):
            awaited = ", ".join(sorted(state.wait))
            problems.append(
                f"wait state {state.name} has no external row on {awaited} and no stays"
            )
    return problems


def _reach_problems(chart: Statechart, ix: _Index) -> list[str]:
    """Every state is entered by the initial entry or by some row."""
    reached = set(_entry_order(ix, [ix.root]))
    frontier = list(reached)
    while frontier:
        state = frontier.pop()
        for row in chart.rows:
            if row.source != state or row.kind != "external":
                continue
            domain = _domain(ix, row.source, _targets(ix, row))
            for target in _targets(ix, row):
                for entered in _entry_order(ix, _entry_path(ix, domain, target)):
                    if entered not in reached:
                        reached.add(entered)
                        frontier.append(entered)
    return [f"state {s} is not reachable" for s in ix.states if s not in reached]


def _fall_through(ix: _Index, state: str, event: str) -> list[str] | None:
    """A configuration below `state` where no active state has a row or a deferral for
    `event`, as its innermost states; None when there is none (§10)."""
    node = ix.states[state]
    if (state, event) in ix.rows or event in node.defer:
        return None
    hole: list[str] = [] if node.regions else [state]
    for region in node.regions:
        holes = [h for s in region.states if (h := _fall_through(ix, s.name, event)) is not None]
        if not holes:
            return None
        hole += holes[0]
    return hole


def _beside(ix: _Index, a: str, b: str) -> bool:
    """True when two regions' walks can reach `a` and `b` in one configuration (§3).

    Either they lie in sibling regions, or one is an ancestor of the other with
    an orthogonal state on the way down, whose other regions reach the ancestor.
    """
    if _crosses_regions(ix, a, b):
        return True
    for upper, lower in ((a, b), (b, a)):
        between = _path(ix, lower)[1:]
        if upper in between:
            between = between[: between.index(upper) + 1]
            if any(len(ix.states[s].regions) > 1 for s in between):
                return True
    return False


def _leaving_warnings(ix: _Index, a: Row, b: Row) -> list[str]:
    """An external row that leaves the source of a non-external row that another
    region's walk reaches (§3): that row is dropped when both are enabled."""
    found: list[str] = []
    for inner, outer in ((a, b), (b, a)):
        if (
            inner.kind != "external"
            and outer.kind == "external"
            and _beside(ix, inner.source, outer.source)
            and _exit_top(ix, outer) in _path(ix, inner.source)
        ):
            found.append(
                f"{inner.id} and {outer.id}: {outer.id} leaves {inner.source}, so {inner.id}"
                " is dropped when both are enabled"
            )
    return found


def chart_warnings(chart: Statechart) -> list[str]:
    """Where two regions' rows for one event can meet badly (§3).

    One of two regions' rows leaves the regions' shared ancestor; an external row
    leaves the source of an internal row on a shared ancestor, which is then
    dropped when both are enabled; or a refusal row and another region's
    consuming row share an event, which raises `AmbiguousTransition` when both
    are enabled.
    """
    ix = _index(chart)
    warnings: list[str] = []
    for a, b in itertools.combinations(chart.rows, 2):
        if a.event != b.event:
            continue
        kinds = {a.kind, b.kind}
        if "refusal" in kinds and "ignored" not in kinds and _beside(ix, a.source, b.source):
            warnings.append(
                f"{a.id} and {b.id}: a refusal and another region's row for {a.event};"
                " both enabled raise AmbiguousTransition"
            )
        if not _crosses_regions(ix, a.source, b.source):
            warnings += _leaving_warnings(ix, a, b)
            continue
        pa, _, common = _split(ix, a.source, b.source)
        shared = pa[common - 1]
        for row in (a, b):
            if row.kind == "external" and _exit_top(ix, row) in _path(ix, shared):
                warnings.append(
                    f"{a.id} and {b.id}: rows for {a.event or 'completion'} in two regions"
                    f" of {shared}, and {row.id} leaves {shared}"
                )
                break
    return warnings
