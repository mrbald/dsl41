"""The old-vs-new oracle harness (statechart-engine ss16, DL-338).

Two oracles, `old` and `new`, are built from the same catalog and the same
semantic switches, and the same inputs go to both. After every input the
harness compares, in this order:

- `outcome`: whether the apply raised, and the exception's class and text;
- `trace`: the trace entries the input added, as the trace contract prints
  them (at, job, transition, cause);
- `emitted`: the events the input emitted (`InputBatch.emitted`);
- `store`: the store rows as canonical bytes: jobs, globals, hosts, the
  timers in `(due, token)` order, `timer_seq`, `consumed` and
  `enqueue_counter`;
- `revisions`: the revisions the input moved, in the order the commit
  returned them (`InputBatch.revisions`);
- `violations` and `command_violations`: what the input broke;
- `pending_timers`: `Oracle.pending_timers()`, in its order;
- `next_timer_due`: the engine's wake-up read.

The store is written as the seal writes its rows: sets sorted, keys sorted,
reservations by bucket, rows by name. Equal stores render the same, so an
order the seal ignores is never a divergence.

At every `SealPoint` in the script, and once at the end, it compares the seal
digests. The digest is the one over the seal's `state` section: the
`SealedState` that `Engine._snapshot` builds from the store, serialized as
`Seal.to_payload` serializes it. Its route table is left empty: the engine
projects it from its own executor id, and the oracle holds none. The other
sections of a seal are the engine's or derive from the rows and the pending
timers, which are compared on their own. `test_oracle_lockstep` pins this
serialization against a real `close_runtime` seal.

The first difference stops the run. Its `Divergence` names the input's
index (0 is the genesis seed), the input, the first field that differs, the
other fields that differ at the same input, and a diff of the two
renderings.

The harness drives each side through `batch` only and reads it through
`trace_since`, `pending_timers` and the store's `job`, `globals_`, `hosts`,
`timers()`, `timer_seq`, `consumed` and `enqueue_counter`. A new oracle
offers the same, built as `factory(catalog, semantics=...)`.

The fourth arm of test_oracle.py's autouse fixture runs every SEM trace
test through `LockstepOracle` when `DSL41_ORACLE_LOCKSTEP` is set: `1` pairs
the oracle with itself; `MODULE:NAME` names the new oracle's factory. That
is the hook the new oracle plugs into.

Command line, from the repository root:

    uv run python tests/oracle_lockstep.py --corpus --seeds 200 --size 50
    uv run python tests/oracle_lockstep.py --seeds 20 --size 200 --switches all
    uv run python tests/oracle_lockstep.py --corpus --mutant cause

`--switches` takes `default`, `all` (the default, every other value of
each switch one switch at a time, and the `SWITCH_PAIRS`), or
`NAME=VALUE[,NAME=VALUE...]`.
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import os
import sys
import time
import types
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from estate_gen import Advance, SealPoint, Step, catalog_script, seeded

import dsl41.oracle
from dsl41.canon import canonical_bytes, digest
from dsl41.cli_run import _trace_line
from dsl41.equiv import equiv_scripts
from dsl41.ir import CatalogIR, lower_catalog, lower_source
from dsl41.ast_jil import parse_file
from dsl41.oracle import InputBatch, Oracle, fit_recursion_limit
from dsl41.oracle_state import Event
from dsl41.seal import SealedHost, SealedState, _canon_ready
from dsl41.semantics import REGISTRY, SemanticSwitches, resolve

LOCKSTEP_ENV = "DSL41_ORACLE_LOCKSTEP"
CORPUS_DIR = Path(__file__).parent / "corpus"
#: the one corpus file that does not lower, on purpose (SEM-31)
CORPUS_UNLOWERABLE = frozenset({"sem31_xor.jil"})
#: the seal instant before any input has set one
EPOCH = datetime(2000, 1, 1)

OracleFactory = Callable[..., Oracle]

FIELDS = (
    "outcome",
    "trace",
    "emitted",
    "store",
    "revisions",
    "violations",
    "command_violations",
    "pending_timers",
    "next_timer_due",
)


# ------------------------------------------------------------------ renderings


def _json(value: object) -> str:
    return canonical_bytes(value).decode()


def describe(step: Step) -> str:
    """One line naming an input."""
    if isinstance(step, SealPoint):
        return "SEAL"
    if isinstance(step, Advance):
        return f"{step.at.isoformat()} ADVANCE"
    payload = " ".join(f"{key}={value}" for key, value in step.payload.items())
    source = f" source={step.source}" if step.source else ""
    return f"{step.at.isoformat()} {step.kind} {payload}{source}"


def _canonical(value: object) -> str:
    """`value` in the seal's canonical form: sets sorted (`_canon_ready`),
    object keys sorted (`canonical_bytes`)."""
    return _json(_canon_ready(value))


def _job_dump(row: Any) -> dict[str, Any]:
    """A job row as the seal writes it: reservations sorted by bucket."""
    dumped: dict[str, Any] = row.model_dump(by_alias=True)
    dumped["reservations"] = sorted(dumped["reservations"], key=lambda held: held["bucket"])
    return dumped


def _host_dump(row: Any) -> dict[str, Any]:
    dumped: dict[str, Any] = row.model_dump(by_alias=True)
    dumped["deadman_s"] = repr(row.deadman_s)  # the canonical grammar has no floats
    return dumped


class _Rows:
    """The store as canonical lines, written as the seal writes its rows:
    sets sorted, keys sorted, reservations by bucket, rows by name. So two
    equal stores render the same whatever order a set iterates in or a row
    was inserted in.

    A row's line is cached by the row's identity. Job, global and host rows
    are frozen all the way down (frozensets, tuples of frozen reservations),
    and every write replaces one, so only the rows an input replaced are
    serialized again; the cache holds each row it keys, so an identity is
    never reused while cached. Timer events are not frozen (an `Event`'s
    payload is a dict), so each is rendered afresh at every input."""

    def __init__(self) -> None:
        self._memo: dict[int, tuple[object, str]] = {}
        self._next: dict[int, tuple[object, str]] = {}

    def _row(self, prefix: str, name: str, row: Any, dump: Callable[[Any], object]) -> str:
        cached = self._memo.get(id(row))
        if cached is None or cached[0] is not row:
            cached = (row, f"{prefix}/{name} {_canonical(dump(row))}")
        self._next[id(row)] = cached
        return cached[1]

    def _rows(self, prefix: str, rows: Any, dump: Callable[[Any], object]) -> list[str]:
        return [self._row(prefix, name, rows[name], dump) for name in sorted(rows)]

    def render(self, oracle: Oracle) -> list[str]:
        store = oracle.store
        self._next = {}
        lines = self._rows("jobs", store.job, _job_dump)
        lines += self._rows("globals", store.globals_, lambda row: row.model_dump(by_alias=True))
        lines += self._rows("hosts", store.hosts, _host_dump)
        self._memo = self._next
        lines += [
            f"timer {_canonical([due, token, ev.model_dump(by_alias=True)])}"
            for due, token, ev in store.timers()
        ]
        lines += [f"consumed/{key} {units}" for key, units in sorted(store.consumed.items())]
        lines.append(f"timer_seq {store.timer_seq}")
        lines.append(f"enqueue_counter {store.enqueue_counter}")
        return lines


def seal_state_payload(state: SealedState) -> dict[str, Any]:
    """The `state` section exactly as `Seal.to_payload` writes it."""
    payload = state.model_dump(by_alias=True)
    payload["jobs"] = {
        name: {**row, "reservations": sorted(row["reservations"], key=lambda h: h["bucket"])}
        for name, row in payload["jobs"].items()
    }
    canon = _canon_ready(payload)
    assert isinstance(canon, dict)
    return canon


def sealed_state(oracle: Oracle, at: datetime) -> SealedState:
    """The `SealedState` `Engine._snapshot` builds from this store at `at`."""
    store = oracle.store
    return SealedState(
        jobs=dict(store.job),
        globals=dict(store.globals_),
        hosts={host_id: SealedHost.of(row) for host_id, row in store.hosts.items()},
        # the engine projects the route table from its own executor id, which
        # has a host row there and none here: not oracle state
        routes={},
        timers=tuple(store.timers()),
        timer_seq=store.timer_seq,
        consumed=dict(store.consumed),
        enqueue_counter=store.enqueue_counter,
        now=at,
    )


def seal_digest(oracle: Oracle, at: datetime) -> tuple[str, dict[str, Any] | None]:
    """The digest of the seal's `state` section, and the section. A state
    the seal model refuses gives the refusal and no section."""
    try:
        payload = seal_state_payload(sealed_state(oracle, at))
    except Exception as exc:  # noqa: BLE001 -- the refusal is the compared value
        return f"unsealable: {type(exc).__name__}: {exc}", None
    return digest({"state": payload}), payload


def seal_lines(sealed: tuple[str, dict[str, Any] | None]) -> list[str]:
    """The digest, then the section one row per line: what a seal
    divergence diffs."""
    found, payload = sealed
    lines = [f"digest {found}"]
    for key, value in (payload or {}).items():
        if isinstance(value, dict):
            lines += [f"{key}/{name} {_json(item)}" for name, item in value.items()]
        else:
            lines.append(f"{key} {_json(value)}")
    return lines


# ------------------------------------------------------------------ the pair


def _own(value: Any) -> Any:
    """A deep copy of a catalog or an input, for one side alone."""
    return value.model_copy(deep=True) if hasattr(value, "model_copy") else value


@dataclass(frozen=True)
class Divergence:
    """The first difference between the two oracles."""

    index: int
    input: str
    field: str
    also: tuple[str, ...]
    old: tuple[str, ...]
    new: tuple[str, ...]
    label: str = ""

    def report(self) -> str:
        where = f" ({self.label})" if self.label else ""
        also = f" (also: {', '.join(self.also)})" if self.also else ""
        diff = difflib.unified_diff(list(self.old), list(self.new), "old", "new", n=2, lineterm="")
        return "\n".join(
            [
                f"lockstep divergence at input {self.index}{where}",
                f"input: {self.input}",
                f"field: {self.field}{also}",
                *diff,
            ]
        )


class LockstepDivergence(AssertionError):
    """Raised by `LockstepOracle` at the first divergence."""


def _fork(oracle: Oracle) -> Oracle:
    """A copy of one side before an input. A `LockstepOracle` is forked as
    the plain oracle it is, not as a pair."""
    return Oracle.fork(oracle) if isinstance(oracle, LockstepOracle) else oracle.fork()


def _alike(old: Exception | None, new: Exception | None) -> bool:
    """Both sides raised the same: the same type and message, or a
    RecursionError on each, bare or converted (`CascadeDepthError`): where
    each met its fitted limit is the stated limit of the comparison."""
    if old is None or new is None:
        return False
    if isinstance(old, RecursionError) and isinstance(new, RecursionError):
        return True
    return type(old) is type(new) and str(old) == str(new)


@dataclass
class _Applied:
    batch: InputBatch
    error: Exception | None
    observed: dict[str, list[str]] = field(default_factory=dict)


class _Side:
    """One oracle and what the harness keeps about it."""

    def __init__(self, oracle: Oracle) -> None:
        self.oracle = oracle
        self.rows = _Rows()
        self.traced = 0

    def apply(self, step: Event | Advance) -> _Applied:
        at = step.at
        batch = self.oracle.batch(at)
        error: Exception | None = None
        try:
            with batch:
                if isinstance(step, Event):
                    batch.feed(step)
        except Exception as exc:  # noqa: BLE001 -- compared, then re-raised by the arm
            error = exc
        return _Applied(batch, error)

    def restore(self, fork: Oracle, emitted: int) -> None:
        """Put this side's oracle back as `fork`, a fork taken before the
        input, in place, since a `LockstepOracle` is its own old side: the
        trace and the emitted log are cut back to their length then (a
        fork's start empty), and a chart oracle's store asks this oracle
        for its job instances again."""
        oracle = self.oracle
        trace, out = oracle._trace[: self.traced], oracle._emitted[:emitted]
        kept = {name: value for name, value in vars(oracle).items() if name == "_lockstep"}
        vars(oracle).update(vars(fork))
        vars(oracle).update(kept)
        oracle._trace, oracle._emitted = trace, out
        if oracle.store.job_instance is not None:
            oracle.store.job_instance = getattr(oracle, "_job_instance")  # noqa: B009

    def observe(self, batch: InputBatch | None, error: Exception | None) -> dict[str, list[str]]:
        oracle = self.oracle
        self.traced, entries = oracle.trace_since(self.traced)
        observed = {
            "outcome": [] if error is None else [f"{type(error).__name__}: {error}"],
            "trace": [_trace_line(entry) for entry in entries],
            "store": self.rows.render(oracle),
            "pending_timers": [
                f"{due.isoformat()} {job} {kind}" for due, job, kind in oracle.pending_timers()
            ],
            "next_timer_due": [str(oracle.next_timer_due())],
        }
        if batch is not None:
            observed["emitted"] = [_canonical(ev.model_dump(by_alias=True)) for ev in batch.emitted]
            observed["revisions"] = [f"{key} {rev}" for key, rev in batch.revisions.items()]
            observed["violations"] = [f"{s} {v!r}" for s, v in batch.violations]
            observed["command_violations"] = [f"{s} {v!r}" for s, v in batch.command_violations]
        return observed


class Lockstep:
    """Two oracles driven in lockstep. `step` applies one input to both and
    returns the old side's result and the divergence, if any."""

    def __init__(self, old: Oracle, new: Oracle, *, label: str = "") -> None:
        self.old, self.new = _Side(old), _Side(new)
        self.label = label
        self.index = 0
        self.now: datetime | None = None
        self.inputs = 0
        self.seals = 0
        self.unsealable = False
        #: inputs on which both sides raised alike (`step`)
        self.raised_alike = 0
        #: the time half of such an input raised alike too: the engine stops
        self.time_half_stopped = False
        #: the time spent observing and comparing, the applies excluded
        self.compare_seconds = 0.0

    def fork(self, old: Oracle) -> Lockstep:
        """This pair with `old`, a fork of the old side, and a fork of the
        new side. A fork's trace starts empty."""
        twin = Lockstep(old, self.new.oracle.fork(), label=self.label)
        twin.index, twin.now = self.index, self.now
        return twin

    def genesis(self) -> Divergence | None:
        """Compare the two stores as their constructors left them."""
        started = time.perf_counter()
        old = self.old.observe(None, None)
        new = self.new.observe(None, None)
        found = self._compare("<genesis>", old, new)
        self.compare_seconds += time.perf_counter() - started
        return found

    def step(self, step: Step) -> tuple[_Applied | None, Divergence | None]:
        """Apply one input to both sides and compare what they leave.

        An input on which both sides raise alike (`_alike`) agrees on its
        attempt half, whose partial writes are not compared: the engine
        commits none of them. A control input that raises is refused and
        commits nothing; an engine-made one is quarantined, its rejection
        logged with the error's text and its time half committed; the engine
        stops only when the time half itself raises. So both sides are put
        back as they were before the input (`_Side.restore`), the time half
        is applied alone on both and compared as any step, and the script
        goes on, except where the time half raises alike on both too: the
        engine stops there (`TimeHalfStop`), and so does the pair
        (`time_half_stopped`, no seal). One side raising, or the two raising
        differently, is compared as any input is: the outcome names both
        errors."""
        self.index += 1
        if isinstance(step, SealPoint):
            return None, self.seal()
        self.inputs += 1
        self.now = step.at
        # each side gets its own copy: an oracle that edits its input in
        # place must not reach the other side or a later run of the script
        forks = [
            (side, _fork(side.oracle), len(side.oracle._emitted)) for side in (self.old, self.new)
        ]
        old = self.old.apply(_own(step))
        new = self.new.apply(_own(step))
        started = time.perf_counter()
        described = describe(step)
        if isinstance(step, Event) and _alike(old.error, new.error):
            self.raised_alike += 1
            for side, fork, emitted in forks:
                side.restore(fork, emitted)
            raised = old
            old = self.old.apply(Advance(step.at))
            new = self.new.apply(Advance(step.at))
            described += f" (both raised {type(raised.error).__name__}; its time half alone)"
            raised.observed = self.old.observe(old.batch, old.error)
            new.observed = self.new.observe(new.batch, new.error)
            if _alike(old.error, new.error):
                self.time_half_stopped = True
                self.compare_seconds += time.perf_counter() - started
                return raised, None
            found = self._compare(described, raised.observed, new.observed)
            self.compare_seconds += time.perf_counter() - started
            return raised, found
        old.observed = self.old.observe(old.batch, old.error)
        new.observed = self.new.observe(new.batch, new.error)
        found = self._compare(described, old.observed, new.observed)
        self.compare_seconds += time.perf_counter() - started
        return old, found

    def seal(self) -> Divergence | None:
        """Compare the seal digests at the last input's instant."""
        self.seals += 1
        at = self.now or EPOCH
        old = seal_digest(self.old.oracle, at)
        new = seal_digest(self.new.oracle, at)
        self.unsealable = old[1] is None
        if old[0] == new[0]:
            return None
        return Divergence(
            self.index,
            f"SEAL at {at.isoformat()}",
            "seal",
            (),
            tuple(seal_lines(old)),
            tuple(seal_lines(new)),
            self.label,
        )

    def _compare(
        self, described: str, old: dict[str, list[str]], new: dict[str, list[str]]
    ) -> Divergence | None:
        differing = [name for name in FIELDS if old.get(name) != new.get(name)]
        if not differing:
            return None
        first = differing[0]
        return Divergence(
            self.index,
            described,
            first,
            tuple(differing[1:]),
            tuple(old.get(first, ())),
            tuple(new.get(first, ())),
            self.label,
        )


@dataclass
class RunResult:
    """What one lockstep run over one script found."""

    label: str
    inputs: int
    seals: int
    seconds: float
    compare_seconds: float
    divergence: Divergence | None
    unsealable: int = 0
    raised_alike: int = 0
    time_half_stopped: int = 0


def run(
    catalog: CatalogIR,
    script: Iterable[Step],
    *,
    new: OracleFactory = Oracle,
    old: OracleFactory = Oracle,
    semantics: SemanticSwitches | None = None,
    label: str = "",
) -> RunResult:
    """Drive `old` and `new` over `script` and stop at the first divergence.
    The end of the script is a seal point. Each side is built on its own
    copy of `catalog`: the IR is mutable and read lazily, so an oracle that
    wrote to a shared catalog would make the other side agree with it."""
    started = time.perf_counter()
    pair = Lockstep(
        old(_own(catalog), semantics=semantics),
        new(_own(catalog), semantics=semantics),
        label=label,
    )
    # the larger of the two sides' fits: the limit is never lowered
    for side in (pair.old, pair.new):
        fit_recursion_limit(len(catalog.jobs), type(side.oracle))
    found = pair.genesis()
    for step in script:
        if found is not None or pair.time_half_stopped:
            break
        _, found = pair.step(step)
    if found is None and not pair.time_half_stopped:
        found = pair.seal()
    return RunResult(
        label,
        pair.inputs,
        pair.seals,
        time.perf_counter() - started,
        pair.compare_seconds,
        found,
        int(pair.unsealable),
        pair.raised_alike,
        int(pair.time_half_stopped),
    )


# ------------------------------------------------------------------ the arm


def new_oracle_factory(value: str | None = None) -> OracleFactory | None:
    """The new oracle `DSL41_ORACLE_LOCKSTEP` names: None when unset, empty
    or `0`; the current `Oracle` for `1`; or the factory at `MODULE:NAME`.
    Any other value is refused by name."""
    value = os.environ.get(LOCKSTEP_ENV, "") if value is None else value
    if value in ("", "0"):
        return None
    if value == "1":
        return Oracle
    module, sep, name = value.partition(":")
    if not (sep and module and name):
        raise ValueError(f"{LOCKSTEP_ENV}={value!r}: expected 0, 1 or MODULE:NAME")
    factory: OracleFactory = getattr(importlib.import_module(module), name)
    return factory


class LockstepOracle(Oracle):
    """An Oracle whose every input is also applied to a new oracle and
    compared. Reads go to this one, the old one; the first divergence
    raises `LockstepDivergence` with the report."""

    def __init__(
        self, catalog: CatalogIR, *, new: OracleFactory | None = None, **kwargs: Any
    ) -> None:
        super().__init__(catalog, **kwargs)
        twin = (new or new_oracle_factory() or Oracle)(_own(catalog), **kwargs)
        self._lockstep = Lockstep(self, twin)
        self._raise(self._lockstep.genesis())

    def fork(self) -> LockstepOracle:
        """A paired fork: this oracle's fork and the new oracle's fork, in
        lockstep, so a dry apply on it reaches neither original."""
        twin = cast(LockstepOracle, Oracle.fork(self))
        twin._lockstep = self._lockstep.fork(twin)
        return twin

    @property
    def twin(self) -> Oracle:
        """The new oracle, for a test that changes both outside an input."""
        return self._lockstep.new.oracle

    def feed(self, ev: Event) -> list[Event]:
        return self._apply(ev)

    def advance(self, now: datetime) -> list[Event]:
        return self._apply(Advance(now))

    def check_seal(self) -> None:
        """Compare the seal digests now."""
        self._raise(self._lockstep.seal())

    def _apply(self, step: Event | Advance) -> list[Event]:
        applied, found = self._lockstep.step(step)
        self._raise(found)
        assert applied is not None
        if applied.error is not None:
            raise applied.error
        return applied.batch.emitted

    @staticmethod
    def _raise(found: Divergence | None) -> None:
        if found is not None:
            raise LockstepDivergence(found.report())


# ------------------------------------------------------------------ mutants

#: One-line faults for the mutant check: (the line as oracle.py has it, its
#: replacement). Each must occur exactly once.
MUTANTS: Mapping[str, tuple[str, str]] = {
    # a swapped cause string
    "cause": ('cause="injected STATUS", exit_code=code', 'cause="injected status", exit_code=code'),
    # a dropped flag clear: OFF_HOLD leaves the hold on
    "hold-clear": ("            self.store.move_flag(job, HOLD_OFF)\n", "            pass\n"),
}
_MUTANT_MODULES: dict[str, types.ModuleType] = {}


def mutant_factory(name: str) -> OracleFactory:
    """The `Oracle` of a copy of oracle.py with the mutant's one line changed."""
    module = _MUTANT_MODULES.get(name)
    if module is None:
        old, new = MUTANTS[name]
        path = Path(dsl41.oracle.__file__)
        source = path.read_text(encoding="utf-8")
        if source.count(old) != 1:
            raise ValueError(f"mutant {name!r}: its line occurs {source.count(old)} times")
        module = types.ModuleType(f"dsl41_oracle_mutant_{name.replace('-', '_')}")
        sys.modules[module.__name__] = module
        exec(
            compile(source.replace(old, new), f"<oracle.py, mutant {name}>", "exec"),
            module.__dict__,
        )
        _MUTANT_MODULES[name] = module
    factory: OracleFactory = module.Oracle
    return factory


# ------------------------------------------------------------------ inputs


#: Switches that reach the oracle only together with another: the oracle
#: compiles an extended calendar, so reads `wekr-first-week`, only in the
#: queued recheck, which `queued-recheck=0` turns off. Each pair is the
#: other value of the first switch under each value of the second that lets
#: it through; under the default the pair is the single-switch row.
SWITCH_PAIRS: tuple[tuple[str, str], ...] = (("wekr-first-week", "queued-recheck"),)


def switch_matrix() -> list[dict[str, str]]:
    """The defaults, then every other value of each switch, one at a time,
    then the `SWITCH_PAIRS`."""
    matrix: list[dict[str, str]] = [{}]
    for name, switch in REGISTRY.items():
        matrix += [{name: value} for value in switch.values if value != switch.default]
    for name, through in SWITCH_PAIRS:
        gate = REGISTRY[through]
        matrix += [
            {name: value, through: opened}
            for value in REGISTRY[name].values
            if value != REGISTRY[name].default
            for opened in gate.values
            if opened != gate.default
        ]
    return matrix


def parse_switches(text: str) -> list[dict[str, str]]:
    if text == "all":
        return switch_matrix()
    if text == "default":
        return [{}]
    overrides = dict(item.split("=", 1) for item in text.split(","))
    resolve(overrides)  # refuses an unknown switch or value
    return [overrides]


def _switch_label(overrides: Mapping[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in overrides.items()) or "defaults"


@dataclass(frozen=True)
class Job:
    """One catalog and one script to run in lockstep. `switches` fill the
    switches a matrix row leaves out, for a directed estate that needs a
    value other than the default to reach its cells; a row that sets one
    wins, so the job runs under every value the matrix names."""

    label: str
    catalog: CatalogIR
    script: tuple[Step, ...]
    switches: Mapping[str, str] = field(default_factory=dict)


def corpus_jobs(scripts: int = 3, length: int = 40) -> list[Job]:
    """Every lowerable corpus file, and the whole corpus as one catalog, each
    with `scripts` scripts from tier (c)'s generator and as many from this
    harness's."""
    files = sorted(p for p in CORPUS_DIR.glob("*.jil") if p.name not in CORPUS_UNLOWERABLE)
    catalogs = [(p.name, lower_catalog([parse_file(p)])) for p in files]
    catalogs.append(("<whole corpus>", lower_catalog([parse_file(p) for p in files])))
    jobs: list[Job] = []
    for name, catalog in catalogs:
        if not catalog.jobs:
            continue
        for index, events in enumerate(equiv_scripts(catalog, scripts=scripts, seed=41)):
            jobs.append(Job(f"{name} tier-c script {index}", catalog, tuple(events)))
        for seed in range(scripts):
            script = catalog_script(catalog, seed, length)
            jobs.append(Job(f"{name} script seed {seed}", catalog, tuple(script)))
    return jobs


#: A directed estate for the `SWITCH_PAIRS` rows: random estates almost
#: never compile an extended calendar. 2014-01-01 is a Wednesday, so the
#: week-of-month rule `WEKRmon#02` holds 2014-01-06..12 under
#: `wekr-first-week=partial` and 2014-01-13..19 under `first-full`. Two
#: waiters queue on one-unit resources and their holders end on 2014-01-06:
#: `wk_skip` excludes the calendar's days (read under `queued-recheck` 1
#: and 2), `wk_only` runs on them (read under 2).
WEKR_JIL = """extended_calendar: wk_rule
condition: WEKRmon#02

insert_resource: WK_A
res_type: R
amount: 1

insert_resource: WK_B
res_type: R
amount: 1

insert_job: wk_hold_a
job_type: c
machine: m1
command: x
resources: (WK_A, QUANTITY=1, FREE=A)

insert_job: wk_hold_b
job_type: c
machine: m1
command: x
resources: (WK_B, QUANTITY=1, FREE=A)

insert_job: wk_skip
job_type: c
machine: m1
command: x
resources: (WK_A, QUANTITY=1)
date_conditions: 1
start_times: "08:00"
days_of_week: all
exclude_calendar: wk_rule

insert_job: wk_only
job_type: c
machine: m1
command: x
resources: (WK_B, QUANTITY=1)
date_conditions: 1
start_times: "08:00"
run_calendar: wk_rule
"""


def _wekr_script() -> tuple[Step, ...]:
    def event(at: datetime, kind: str, job: str, **payload: object) -> Event:
        return Event.model_validate({"at": at, "kind": kind, "payload": {"job": job, **payload}})

    day5, day6 = datetime(2014, 1, 5, 8, 0), datetime(2014, 1, 6, 8, 0)
    minute = timedelta(minutes=1)
    return (
        event(day5 - minute, "STARTJOB", "wk_hold_a"),
        event(day5, "STARTJOB", "wk_skip"),
        event(day6 - minute, "STARTJOB", "wk_hold_b"),
        event(day6, "STARTJOB", "wk_only"),
        event(day6 + minute, "STATUS", "wk_hold_a", status="SUCCESS"),
        event(day6 + 2 * minute, "STATUS", "wk_hold_b", status="SUCCESS"),
        SealPoint(),
    )


#: A directed estate for a box restarted inside its member's own box rules by
#: a STATUS that ends no run (DL-341): `rb` restarts on its own success,
#: and its member `rm` takes a unit when it runs. The seeded sample reaches
#: this rarely (seed 2870 alone of seeds 0 to 2999).
RESTART_JIL = """insert_resource: RR
res_type: R
amount: 1

insert_job: rb
job_type: b
condition: s(rb)

insert_job: rm
job_type: c
machine: m1
command: x
box_name: rb
resources: (RR, QUANTITY=1, FREE=Y)
"""


def _restart_scripts() -> list[tuple[Step, ...]]:
    def event(minutes: int, kind: str, job: str, **payload: object) -> Event:
        at = EPOCH + timedelta(minutes=minutes)
        return Event.model_validate({"at": at, "kind": kind, "payload": {"job": job, **payload}})

    never_run = (event(1, "STATUS", "rm", exit_code=0), SealPoint())
    after_a_failure = (
        event(1, "FORCE_STARTJOB", "rb"),
        event(2, "STATUS", "rm", status="FAILURE"),
        event(3, "STATUS", "rm", status="SUCCESS"),
        SealPoint(),
    )
    return [never_run, after_a_failure]


#: A directed estate for the kill and timer cells the seeded sample does
#: not reach (DL-343). `kt_hold` holds the one unit.
#: - `kt_wait` is armed on hold, then queued by OFF_HOLD's retry: a kill on a
#:   waiter whose arm is latched (job.217).
#: - `kt_box` is a box that declares the unit (the generator gives boxes no
#:   resources): it queues behind `kt_hold` (a kill on a queued box, armed or
#:   not), or runs holding it with its terminator member `kt_bm` (a kill, a
#:   term_run_time, each settling the box's units).
#: - `kt_scan` queues behind `kt_hold` on 2000-01-01, which `kt_never`
#:   excludes with every day of the scan's two-year span, so the queued
#:   recheck sends it back to INACTIVE with a continuation at the span's end,
#:   2002-01-02 (DL-257): it resumes, or is refused because the job ran since
#:   or is no longer INACTIVE. `kt_mem` does the same inside box `kb`, so its
#:   continuation carries the box's run (DL-246) and can be box-stale.
#:   These need `queued-recheck` 1 or 2.
KILL_TIMER_JIL = (
    "calendar: kt_never\n"
    + "\n".join(f"{EPOCH.date() + timedelta(days=d):%m/%d/%Y}" for d in range(760))
    + """

insert_resource: KT_R
res_type: R
amount: 1

insert_job: kt_hold
job_type: c
machine: m1
command: x
resources: (KT_R, QUANTITY=1, FREE=Y)

insert_job: kt_wait
job_type: c
machine: m1
command: x
resources: (KT_R, QUANTITY=1, FREE=Y)
date_conditions: 1
days_of_week: all
start_times: "08:00"

insert_job: kt_box
job_type: b
resources: (KT_R, QUANTITY=1, FREE=Y)
term_run_time: 5
date_conditions: 1
days_of_week: all
start_times: "08:00"

insert_job: kt_bm
job_type: c
machine: m1
command: x
box_name: kt_box
job_terminator: 1

insert_job: kt_scan
job_type: c
machine: m1
command: x
resources: (KT_R, QUANTITY=1, FREE=Y)
date_conditions: 1
days_of_week: mo
exclude_calendar: kt_never
run_window: "07:00-10:00"

insert_job: kb
job_type: b

insert_job: kt_mem
job_type: c
machine: m1
command: x
box_name: kb
resources: (KT_R, QUANTITY=1, FREE=Y)
date_conditions: 1
days_of_week: mo
exclude_calendar: kt_never
run_window: "07:00-10:00"
"""
)
#: The scripts of `KILL_TIMER_JIL` that reach a scan continuation, and so
#: need `queued-recheck` 1 or 2.
KILL_TIMER_RESCANS = frozenset(
    {
        "rescan resumes",
        "rescan after a run",
        "rescan after a status",
        "rescan after a run, INACTIVE again",
        "member rescan, box restarted and member ran",
        "member rescan, box restarted",
    }
)


def _kill_timer_scripts() -> dict[str, tuple[Step, ...]]:
    def event(at: datetime, kind: str, job: str, **payload: object) -> Event:
        return Event.model_validate({"at": at, "kind": kind, "payload": {"job": job, **payload}})

    t0 = EPOCH + timedelta(hours=8)
    minute = timedelta(minutes=1)
    bound = datetime(2002, 1, 2)  # the continuation: midnight of the span's last day
    held = event(t0, "STARTJOB", "kt_hold")
    queued = (
        held,
        event(t0, "STARTJOB", "kt_scan"),
        event(t0 + minute, "STATUS", "kt_hold", status="SUCCESS"),
        SealPoint(),
    )
    member = (
        held,
        event(t0, "FORCE_STARTJOB", "kb"),
        event(t0, "STARTJOB", "kt_mem"),
        event(t0 + minute, "STATUS", "kt_hold", status="SUCCESS"),
        SealPoint(),
    )
    return {
        "armed waiter killed": (
            held,
            event(t0 + minute, "ON_HOLD", "kt_wait"),
            event(t0 + minute, "STARTJOB", "kt_wait"),
            event(t0 + 2 * minute, "OFF_HOLD", "kt_wait"),
            event(t0 + 3 * minute, "KILLJOB", "kt_wait"),
            SealPoint(),
        ),
        "queued box killed": (
            held,
            event(t0 + minute, "STARTJOB", "kt_box"),
            event(t0 + 2 * minute, "KILLJOB", "kt_box"),
            SealPoint(),
        ),
        "armed queued box killed": (
            held,
            event(t0 + minute, "ON_HOLD", "kt_box"),
            event(t0 + minute, "STARTJOB", "kt_box"),
            event(t0 + 2 * minute, "OFF_HOLD", "kt_box"),
            event(t0 + 3 * minute, "KILLJOB", "kt_box"),
            SealPoint(),
        ),
        "running box with units killed": (
            event(t0, "STARTJOB", "kt_box"),
            event(t0 + minute, "KILLJOB", "kt_box"),
            SealPoint(),
        ),
        "running box with units past term_run_time": (
            event(t0, "STARTJOB", "kt_box"),
            Advance(t0 + 6 * minute),
            SealPoint(),
        ),
        "rescan resumes": (
            *queued,
            Advance(bound + minute),
            Advance(datetime(2002, 2, 4, 7, 1)),
            SealPoint(),
        ),
        "rescan after a run": (
            *queued,
            event(t0 + 2 * minute, "FORCE_STARTJOB", "kt_scan"),
            event(t0 + 3 * minute, "STATUS", "kt_scan", status="SUCCESS"),
            Advance(bound + minute),
        ),
        "rescan after a status": (
            *queued,
            event(t0 + 2 * minute, "STATUS", "kt_scan", status="FAILURE"),
            Advance(bound + minute),
        ),
        "rescan after a run, INACTIVE again": (
            *queued,
            event(t0 + 2 * minute, "FORCE_STARTJOB", "kt_scan"),
            event(t0 + 3 * minute, "STATUS", "kt_scan", status="SUCCESS"),
            event(t0 + 4 * minute, "STATUS", "kt_scan", status="INACTIVE"),
            Advance(bound + minute),
        ),
        "member rescan, box restarted and member ran": (
            *member,
            event(t0 + 2 * minute, "STATUS", "kb", status="SUCCESS"),
            event(t0 + 3 * minute, "FORCE_STARTJOB", "kb"),
            event(t0 + 3 * minute, "FORCE_STARTJOB", "kt_mem"),
            event(t0 + 4 * minute, "STATUS", "kt_mem", status="SUCCESS"),
            Advance(bound + minute),
        ),
        "member rescan, box restarted": (
            *member,
            event(t0 + 2 * minute, "STATUS", "kb", status="SUCCESS"),
            event(t0 + 3 * minute, "STARTJOB", "kt_hold"),
            event(t0 + 3 * minute, "FORCE_STARTJOB", "kb"),
            Advance(bound + minute),
        ),
    }


#: A directed estate for the flag cells (DL-344) that the seeded sample
#: reaches rarely or never. `fl_hold` holds the one unit, so `fl_wait` and
#: the box `fl_box` queue behind it (the generator gives boxes no resources).
#: `fl_q`'s members can be busy while it is not running: `fl_qm` forced and
#: queued, `fl_qs` injected STARTING. `fl_run` runs with `fl_rn`; its member
#: `fl_rm` is iced first, and its subbox `fl_sub` waits on a job that never
#: runs, so it stays INACTIVE inside a RUNNING box, and its ice is the box's
#: completion moment (DL-285). `fl_dated` and `fl_box` arm on a held start
#: tick.
FLAG_JIL = """insert_resource: FL_R
res_type: R
amount: 1

insert_job: fl_hold
job_type: c
machine: m1
command: x
resources: (FL_R, QUANTITY=1, FREE=Y)

insert_job: fl_wait
job_type: c
machine: m1
command: x
resources: (FL_R, QUANTITY=1, FREE=Y)

insert_job: fl_job
job_type: c
machine: m1
command: x

insert_job: fl_dated
job_type: c
machine: m1
command: x
date_conditions: 1
days_of_week: all
start_times: "08:00"

insert_job: fl_box
job_type: b
resources: (FL_R, QUANTITY=1, FREE=Y)
date_conditions: 1
days_of_week: all
start_times: "08:00"

insert_job: fl_bm
job_type: c
machine: m1
command: x
box_name: fl_box

insert_job: fl_q
job_type: b

insert_job: fl_qm
job_type: c
machine: m1
command: x
box_name: fl_q
resources: (FL_R, QUANTITY=1, FREE=Y)

insert_job: fl_qs
job_type: c
machine: m1
command: x
box_name: fl_q

insert_job: fl_run
job_type: b

insert_job: fl_rm
job_type: c
machine: m1
command: x
box_name: fl_run

insert_job: fl_rn
job_type: c
machine: m1
command: x
box_name: fl_run

insert_job: fl_sub
job_type: b
box_name: fl_run
condition: s(fl_never)

insert_job: fl_sm
job_type: c
machine: m1
command: x
box_name: fl_sub

insert_job: fl_never
job_type: c
machine: m1
command: x
"""


def _flag_scripts() -> dict[str, tuple[Step, ...]]:
    def event(at: datetime, kind: str, job: str, **payload: object) -> Event:
        return Event.model_validate({"at": at, "kind": kind, "payload": {"job": job, **payload}})

    t0 = EPOCH + timedelta(hours=8)
    minute = timedelta(minutes=1)

    def at(n: int) -> datetime:
        return t0 + n * minute

    held = event(t0, "STARTJOB", "fl_hold")
    return {
        "flags on live jobs ignored": (
            event(t0, "STARTJOB", "fl_job"),
            event(at(1), "ON_ICE", "fl_job"),
            event(at(1), "ON_HOLD", "fl_job"),
            event(at(1), "ON_NOEXEC", "fl_job"),
            event(at(2), "STATUS", "fl_hold", status="STARTING"),
            event(at(3), "ON_ICE", "fl_hold"),
            event(at(3), "ON_HOLD", "fl_hold"),
            event(at(3), "ON_NOEXEC", "fl_hold"),
            SealPoint(),
        ),
        "ON_NOEXEC on an iced job ignored": (
            event(t0, "ON_ICE", "fl_job"),
            event(at(1), "ON_ICE", "fl_job"),
            event(at(1), "ON_NOEXEC", "fl_job"),
            event(at(2), "OFF_ICE", "fl_job"),
            event(at(2), "OFF_ICE", "fl_job"),
            SealPoint(),
        ),
        "ON_NOEXEC on boxes with busy members ignored": (
            held,
            event(at(1), "FORCE_STARTJOB", "fl_qm"),
            event(at(2), "ON_NOEXEC", "fl_q"),
            event(at(3), "STARTJOB", "fl_box"),
            event(at(4), "FORCE_STARTJOB", "fl_bm"),
            event(at(5), "ON_NOEXEC", "fl_box"),
            SealPoint(),
        ),
        "ON_NOEXEC on a box with a STARTING member ignored": (
            event(t0, "STATUS", "fl_qs", status="STARTING"),
            event(at(1), "ON_NOEXEC", "fl_q"),
            SealPoint(),
        ),
        "ON_HOLD and ON_ICE on waiters": (
            held,
            event(t0, "STARTJOB", "fl_wait"),
            event(at(1), "ON_HOLD", "fl_wait"),
            event(at(1), "ON_HOLD", "fl_wait"),
            event(at(2), "STATUS", "fl_hold", status="SUCCESS"),
            event(at(3), "OFF_HOLD", "fl_wait"),
            event(at(4), "STARTJOB", "fl_hold"),
            event(at(5), "ON_ICE", "fl_hold"),
            event(at(6), "STATUS", "fl_wait", status="SUCCESS"),
            SealPoint(),
        ),
        "ON_NOEXEC on waiters": (
            held,
            event(t0, "STARTJOB", "fl_wait"),
            event(at(1), "ON_HOLD", "fl_wait"),
            event(at(2), "ON_NOEXEC", "fl_wait"),
            event(at(3), "STARTJOB", "fl_job"),
            event(at(3), "OFF_NOEXEC", "fl_wait"),
            event(at(4), "STARTJOB", "fl_wait"),
            event(at(5), "ON_NOEXEC", "fl_wait"),
            SealPoint(),
        ),
        "ON_NOEXEC on a FAILED job": (
            event(t0, "STATUS", "fl_job", exit_code=1),
            event(at(1), "ON_NOEXEC", "fl_job"),
            event(at(2), "OFF_NOEXEC", "fl_job"),
            event(at(2), "OFF_NOEXEC", "fl_job"),
            SealPoint(),
        ),
        "flags on a queued box": (
            held,
            event(at(1), "STARTJOB", "fl_box"),
            event(at(2), "ON_HOLD", "fl_box"),
            event(at(3), "OFF_HOLD", "fl_box"),
            event(at(4), "DISARM", "fl_box"),
            event(at(5), "ON_ICE", "fl_box"),
            event(at(6), "OFF_NOEXEC", "fl_box"),
            SealPoint(),
        ),
        "ON_NOEXEC on a queued box": (
            held,
            event(at(1), "STARTJOB", "fl_box"),
            event(at(2), "ON_NOEXEC", "fl_box"),
            event(at(3), "STATUS", "fl_hold", status="SUCCESS"),
            SealPoint(),
        ),
        "ON_NOEXEC on a STARTING box": (
            event(t0, "STATUS", "fl_qs", status="INACTIVE", exit_code=3),
            event(at(1), "STATUS", "fl_q", status="STARTING"),
            event(at(2), "ON_NOEXEC", "fl_q"),
            event(at(3), "OFF_NOEXEC", "fl_q"),
            SealPoint(),
        ),
        "ON_NOEXEC on a STARTING box that holds a unit": (
            event(t0, "ON_HOLD", "fl_bm"),
            event(t0, "STARTJOB", "fl_box"),
            event(at(1), "STATUS", "fl_box", status="STARTING"),
            event(at(2), "STARTJOB", "fl_wait"),
            event(at(3), "ON_NOEXEC", "fl_box"),
            event(at(4), "OFF_HOLD", "fl_bm"),
            SealPoint(),
        ),
        "ON_NOEXEC on a held idle box": (
            event(t0, "ON_HOLD", "fl_q"),
            event(t0, "ON_HOLD", "fl_qs"),
            event(at(1), "ON_NOEXEC", "fl_q"),
            SealPoint(),
        ),
        "DISARM on an armed job and box": (
            event(t0 - minute, "ON_HOLD", "fl_dated"),
            event(t0 - minute, "ON_HOLD", "fl_box"),
            event(t0, "STARTJOB", "fl_dated"),
            event(t0, "STARTJOB", "fl_box"),
            event(at(1), "DISARM", "fl_dated"),
            event(at(1), "DISARM", "fl_dated"),
            event(at(2), "DISARM", "fl_box"),
            event(at(3), "OFF_HOLD", "fl_box"),
            SealPoint(),
        ),
        "OFF_ICE in a running box, ON_NOEXEC on its idle subbox": (
            event(t0, "ON_ICE", "fl_rm"),
            event(at(1), "FORCE_STARTJOB", "fl_run"),
            event(at(2), "OFF_ICE", "fl_rm"),
            event(at(3), "ON_HOLD", "fl_sm"),
            event(at(4), "ON_NOEXEC", "fl_sub"),
            event(at(5), "OFF_NOEXEC", "fl_sub"),
            event(at(6), "STATUS", "fl_rn", status="SUCCESS"),
            SealPoint(),
        ),
        "ON_ICE on the last member a running box waits for": (
            event(t0, "FORCE_STARTJOB", "fl_run"),
            event(at(1), "STATUS", "fl_rm", status="SUCCESS"),
            event(at(1), "STATUS", "fl_rn", status="SUCCESS"),
            event(at(2), "ON_ICE", "fl_sub"),
            SealPoint(),
        ),
        "ON_NOEXEC on a job outside the catalog": (
            event(t0, "ON_HOLD", "fl_ghost"),
            event(at(1), "ON_NOEXEC", "fl_ghost"),
            SealPoint(),
        ),
    }


def directed_jobs() -> list[Job]:
    """The directed estates the matrix runs besides the corpus."""
    restart = lower_source(RESTART_JIL)
    kill_timer = lower_source(KILL_TIMER_JIL)
    flags = lower_source(FLAG_JIL)
    return (
        [Job("<directed wekr>", lower_source(WEKR_JIL), _wekr_script())]
        + [
            Job(f"<directed idle restart {index}>", restart, script)
            for index, script in enumerate(_restart_scripts())
        ]
        + [
            Job(
                f"<directed kill and timer: {name}>",
                kill_timer,
                script,
                {"queued-recheck": "1"} if name in KILL_TIMER_RESCANS else {},
            )
            for name, script in _kill_timer_scripts().items()
        ]
        + [
            Job(f"<directed flags: {name}>", flags, script)
            for name, script in _flag_scripts().items()
        ]
    )


def generated_jobs(seeds: Iterable[int], size: int, min_size: int = 1) -> list[Job]:
    """The seeded estates, each with `min_size` to `size` jobs."""
    jobs = []
    for seed in seeds:
        case = seeded(seed, size, min_size)
        jobs.append(Job(case.label, lower_source(case.jil), case.script))
    return jobs


@dataclass
class Summary:
    runs: int = 0
    inputs: int = 0
    seals: int = 0
    unsealable: int = 0
    raised_alike: int = 0
    time_half_stopped: int = 0
    seconds: float = 0.0
    compare_seconds: float = 0.0
    divergences: list[Divergence] = field(default_factory=list)

    def add(self, result: RunResult) -> None:
        self.runs += 1
        self.inputs += result.inputs
        self.seals += result.seals
        self.unsealable += result.unsealable
        self.raised_alike += result.raised_alike
        self.time_half_stopped += result.time_half_stopped
        self.seconds += result.seconds
        self.compare_seconds += result.compare_seconds
        if result.divergence is not None:
            self.divergences.append(result.divergence)

    def line(self) -> str:
        per_input = self.compare_seconds / self.inputs * 1e6 if self.inputs else 0.0
        return (
            f"{self.runs} runs, {self.inputs} inputs, {self.seals} seal points,"
            f" {len(self.divergences)} divergences, {self.unsealable} unsealable ends,"
            f" {self.raised_alike} inputs both raised alike (time half compared),"
            f" {self.time_half_stopped} stopped where the time half raised on both;"
            f" {self.seconds:.1f} s, comparison {per_input:.0f} us per input"
        )


def run_all(
    jobs: Sequence[Job],
    matrix: Sequence[Mapping[str, str]],
    *,
    new: OracleFactory = Oracle,
    stop_at_first: bool = False,
) -> Summary:
    summary = Summary()
    ran: set[tuple[int, tuple[tuple[str, str], ...]]] = set()
    for row in matrix:
        for index, job in enumerate(jobs):
            # the row wins; a job's own switch fills only one the row leaves
            # out, and a merged row the job has already run under runs once
            # (keyed by the job's place in `jobs`, not its label, which two
            # jobs may share)
            overrides = {**job.switches, **row}
            merged = (index, tuple(sorted(overrides.items())))
            if merged in ran:
                continue
            ran.add(merged)
            label = f"{job.label}; switches {_switch_label(overrides)}"
            semantics = resolve(overrides)
            summary.add(run(job.catalog, job.script, new=new, semantics=semantics, label=label))
            if stop_at_first and summary.divergences:
                return summary
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", action="store_true", help="run the corpus")
    parser.add_argument("--seeds", type=int, default=0, help="generated estates to run")
    parser.add_argument("--first-seed", type=int, default=0)
    parser.add_argument("--size", type=int, default=50, help="the most jobs per estate")
    parser.add_argument("--min-size", type=int, default=1, help="the fewest jobs per estate")
    parser.add_argument("--switches", default="default", help="default, all, or NAME=VALUE,...")
    parser.add_argument("--mutant", choices=sorted(MUTANTS), help="pair the oracle with a mutant")
    parser.add_argument("--new", help="the new oracle's factory, MODULE:NAME")
    args = parser.parse_args(argv)
    new: OracleFactory = Oracle
    if args.mutant:
        new = mutant_factory(args.mutant)
    elif args.new:
        new = new_oracle_factory(args.new) or Oracle
    jobs = corpus_jobs() + directed_jobs() if args.corpus else []
    seeds = range(args.first_seed, args.first_seed + args.seeds)
    jobs += generated_jobs(seeds, args.size, args.min_size)
    summary = run_all(jobs, parse_switches(args.switches), new=new, stop_at_first=True)
    print(summary.line())
    for found in summary.divergences:
        print(found.report())
    return 1 if summary.divergences else 0


if __name__ == "__main__":
    sys.exit(main())
