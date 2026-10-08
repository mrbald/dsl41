"""The shared state-machine core: declared transitions, one check, and hit recording.

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
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal

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


def well_formed[S: str](machine: StateMachine[S]) -> list[str]:
    """The problems with a machine's declaration. An empty list means well formed.

    The machine name matches `[a-z][a-z_]*`, and every transition id is
    `<machine name>.<two or three digits>`, such as `job_status.05`.
    """
    problems: list[str] = []
    if not NAME_PATTERN.fullmatch(machine.name):
        problems.append(f"machine name {machine.name!r} does not match [a-z][a-z_]*")
    prefix = machine.name + "."
    for t in machine.transitions:
        if not (t.id.startswith(prefix) and ID_DIGITS.fullmatch(t.id[len(prefix) :])):
            problems.append(f"transition id {t.id!r} is not {machine.name}.<two or three digits>")
    ids = [t.id for t in machine.transitions]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"duplicate transition id {dup}")
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
