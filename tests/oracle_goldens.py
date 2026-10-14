"""Golden digests of the oracle over the harness's sample (DL-351).

The old-versus-new harness (DL-338) compared two oracles input by input. The
old oracle is gone, so its answers are kept as digests: per input of the
sample, the outcome, the trace lines, the emitted events, the store's rows,
the revisions, the pending timers and the next timer due, and the seal digest
at each seal point, rendered as the harness rendered them. The violation
channel (DL-292) is not digested: its trace lines are left out of the trace
component. The old oracle recorded no violation on the whole sample, and the
oracle the engine builds still writes the channel, so a replay fails on any
run that records one, naming its first entry.

The sample is the harness's three runs:

- `corpus-seeds`: the corpus and the directed estates, then seeds 0 to 199
  at up to 50 jobs, under the default switches;
- `large`: seeds 5000 to 5019 at 150 to 200 jobs;
- `switches`: the corpus, the directed estates and seeds 2000 to 2039 at up
  to 50 jobs, under every row of the switch matrix.

Each run keeps one chain per component: a digest of the previous link and
the component's rendering at the input. A run's golden line holds a digest of
the label, the number of entries (the genesis, each input, each seal point and
the end's seal), a digest of the last links, which decides equality, and two
hex digits of each link at each entry, which name the first entry and
component that differ. A run whose links equal an earlier run's points at it
by its place in the file.

Commands, from the repository root:

    uv run python tests/oracle_goldens.py replay           # the full sample
    uv run python tests/oracle_goldens.py replay --sample large
    uv run python tests/oracle_goldens.py record           # rewrite the file

`replay` exits 1 at a difference, naming the run, the input and the
component. Both replay and record use the oracle the engine builds;
`--factory MODULE:NAME` names another. `record` writes
`tests/oracle_goldens.txt`. A golden that changes is
a decision under DL-235: the entry that changes it names the change and why,
and its STATE_MACHINE_VERSION review follows from it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from estate_gen import Advance, SealPoint, Step
from oracle_lockstep import (
    EPOCH,
    Job,
    _fork,
    _own,
    _Side,
    _switch_label,
    corpus_jobs,
    describe,
    directed_jobs,
    generated_jobs,
    seal_digest,
    seal_lines,
    switch_matrix,
)

import dsl41.oracle
from dsl41.ir import CatalogIR
from dsl41.oracle import Oracle
from dsl41.oracle_chart import ChartOracle
from dsl41.oracle_state import VIOLATION_MARKER, Event
from dsl41.semantics import SemanticSwitches, resolve

GOLDENS = Path(__file__).parent / "oracle_goldens.txt"

OracleFactory = Callable[..., Oracle]

#: The oracle the engine and every replay build, which the goldens hold to.
PRODUCTION: OracleFactory = ChartOracle

#: The compared components, in the order a difference names them.
COMPONENTS = (
    "outcome",
    "trace",
    "emitted",
    "store",
    "revisions",
    "pending_timers",
    "next_timer_due",
    "seal",
)
#: Hex digits kept of each link per entry, and of the digest of the last links.
CODE = 2
LAST = 16

SAMPLES = ("corpus-seeds", "large", "switches")


def sample_jobs(name: str) -> tuple[list[Job], list[dict[str, str]]]:
    """The jobs and the switch rows of one sample."""
    if name == "corpus-seeds":
        return corpus_jobs() + directed_jobs() + generated_jobs(range(200), 50), [{}]
    if name == "large":
        return generated_jobs(range(5000, 5020), 200, 150), [{}]
    if name == "switches":
        return corpus_jobs() + directed_jobs() + generated_jobs(range(2000, 2040), 50), [
            *switch_matrix()
        ]
    raise ValueError(f"unknown sample {name!r}; expected one of {', '.join(SAMPLES)}")


@dataclass(frozen=True)
class Run:
    """One catalog and script under one switch row."""

    sample: str
    label: str
    row: str
    catalog: CatalogIR
    script: tuple[Step, ...]
    semantics: SemanticSwitches


def sample_runs(name: str) -> Iterator[Run]:
    """The runs of one sample in the harness's order: switch rows outer, jobs
    inner; a job's own switches fill a row's gaps, and a merged row a job
    already ran under runs once (`oracle_lockstep.run_all`)."""
    jobs, matrix = sample_jobs(name)
    ran: set[tuple[int, tuple[tuple[str, str], ...]]] = set()
    for row in matrix:
        for index, job in enumerate(jobs):
            overrides = {**job.switches, **row}
            merged = (index, tuple(sorted(overrides.items())))
            if merged in ran:
                continue
            ran.add(merged)
            label = f"{job.label}; switches {_switch_label(overrides)}"
            yield Run(name, label, _switch_label(row), job.catalog, job.script, resolve(overrides))


# ------------------------------------------------------------------ one run


def _link(previous: bytes, lines: Sequence[str]) -> bytes:
    return hashlib.blake2b(previous + "\n".join(lines).encode(), digest_size=16).digest()


@dataclass
class Digests:
    """A run's chains: the inputs' descriptions, each input's codes, the last
    links, and the inputs that recorded a violation."""

    described: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    links: list[bytes] = field(default_factory=lambda: [b""] * len(COMPONENTS))
    violations: int = 0
    #: the first entry whose input recorded a violation, if any
    first_violation: int | None = None

    def add(self, described: str, observed: Mapping[str, Sequence[str]]) -> None:
        self.links = [
            _link(link, observed.get(name, ())) for name, link in zip(COMPONENTS, self.links)
        ]
        self.described.append(described)
        self.codes.append("".join(link.hex()[:CODE] for link in self.links))

    def last(self) -> str:
        return hashlib.blake2b(b"".join(self.links), digest_size=LAST // 2).hexdigest()


def _observe(side: _Side, applied_batch: object, error: Exception | None) -> dict[str, list[str]]:
    """The harness's observation, the violation channel left out."""
    observed = side.observe(applied_batch, error)  # type: ignore[arg-type]
    observed["trace"] = [line for line in observed["trace"] if f" {VIOLATION_MARKER} " not in line]
    observed.pop("violations", None)
    observed.pop("command_violations", None)
    return observed


def _fit(oracle: Oracle, jobs: int) -> None:
    """The old oracle's recursion fit, while the build has one (DL-306)."""
    fit = getattr(dsl41.oracle, "fit_recursion_limit", None)
    if fit is not None:
        fit(jobs, type(oracle))


def digest_run(run: Run, factory: OracleFactory) -> Digests:
    """Drive one oracle over the run's script as the harness drove each side:
    an input that raises is put back and its time half applied alone, as the
    engine quarantines it (`oracle_lockstep.Lockstep.step`)."""
    oracle = factory(_own(run.catalog), semantics=run.semantics)
    _fit(oracle, len(run.catalog.jobs))
    side = _Side(oracle)
    found = Digests()
    found.add("<genesis>", _observe(side, None, None))
    now = None
    for step in run.script:
        if isinstance(step, SealPoint):
            found.add("SEAL", {"seal": seal_lines(seal_digest(side.oracle, now or EPOCH))})
            continue
        now = step.at
        fork, emitted = _fork(side.oracle), len(side.oracle._emitted)
        applied = side.apply(_own(step))
        described = describe(step)
        raised = applied.error
        if raised is not None and isinstance(step, Event):
            side.restore(fork, emitted)
            applied = side.apply(Advance(step.at))
            described += f" (raised {type(raised).__name__}; its time half alone)"
        observed = _observe(side, applied.batch, applied.error)
        if raised is not None:
            observed["outcome"] = [f"{type(raised).__name__}: {raised}"]
        if applied.batch.violations:
            found.violations += 1
            if found.first_violation is None:
                found.first_violation = len(found.codes)
        found.add(described, observed)
        if raised is not None and applied.error is not None:
            break  # the time half raised too: the engine stops here
    found.add("SEAL at the end", {"seal": seal_lines(seal_digest(side.oracle, now or EPOCH))})
    return found


# ------------------------------------------------------------------ the file


@dataclass(frozen=True)
class Golden:
    sample: str
    label: str
    entries: int
    last: str
    codes: tuple[str, ...]


def label_digest(label: str) -> str:
    return hashlib.blake2b(label.encode(), digest_size=4).hexdigest()


def _line(run: Run, found: Digests, seen: dict[tuple[int, str], int], ordinal: int) -> str:
    key = (len(found.codes), found.last())
    codes = f"={seen[key]}" if key in seen else "".join(found.codes)
    seen.setdefault(key, ordinal)
    return f"{label_digest(run.label)}\t{len(found.codes)}\t{found.last()}\t{codes}"


def read_goldens(path: Path = GOLDENS) -> list[Golden]:
    """The golden lines in file order, a pointer resolved to the codes it names."""
    goldens: list[Golden] = []
    width = CODE * len(COMPONENTS)
    sample = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            sample = line.strip("[]")
            continue
        label, entries, last, codes = line.split("\t")
        joined = "".join(goldens[int(codes[1:])].codes) if codes.startswith("=") else codes
        split = tuple(joined[i : i + width] for i in range(0, len(joined), width))
        goldens.append(Golden(sample, label, int(entries), last, split))
    return goldens


_HEADER = """\
# Golden digests of the oracle over the harness's sample (DL-351).
# Written by `uv run python tests/oracle_goldens.py record`; see that module.
# A golden that changes is a decision under DL-235, named in its entry.
# Per run: the label's digest, the entries, the digest of the last links,
# then {code} hex digits per link per entry, or =N for the codes of the Nth
# run in this file. The links, in order: {components}.
"""


@dataclass
class Recorded:
    runs: int = 0
    inputs: int = 0
    violations: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0


def record(factory: OracleFactory, path: Path = GOLDENS) -> Recorded:
    """Write the goldens of every sample from `factory`'s oracle, and count,
    per sample and switch row, the inputs that recorded a violation."""
    started = time.perf_counter()
    lines = [_HEADER.format(components=", ".join(COMPONENTS), code=CODE)]
    seen: dict[tuple[int, str], int] = {}
    summary = Recorded()
    for sample in SAMPLES:
        lines.append(f"[{sample}]\n")
        for run in sample_runs(sample):
            found = digest_run(run, factory)
            lines.append(_line(run, found, seen, summary.runs) + "\n")
            summary.runs += 1
            summary.inputs += len(found.codes)
            key = f"{sample}; {run.row}"
            summary.violations[key] = summary.violations.get(key, 0) + found.violations
    path.write_text("".join(lines), encoding="utf-8")
    summary.seconds = time.perf_counter() - started
    return summary


@dataclass(frozen=True)
class Difference:
    """A run whose digests are not its golden's."""

    sample: str
    label: str
    index: int | None
    input: str
    component: str

    def report(self) -> str:
        at = "an unknown entry" if self.index is None else f"entry {self.index}"
        return (
            f"golden difference in {self.sample}: {self.label}\n"
            f"  at {at}: {self.input}\n  component: {self.component}"
        )


def compare(run: Run, golden: Golden, found: Digests) -> Difference | None:
    """The first input and component where `found` leaves `golden`, if any.
    A run that records a violation differs: the goldens recorded none."""
    if golden.label != label_digest(run.label):
        return Difference(run.sample, run.label, None, "the golden is another run's", "run")
    if found.first_violation is not None:
        index = found.first_violation
        return Difference(run.sample, run.label, index, found.described[index], "violations")
    if found.last() == golden.last and len(found.codes) == golden.entries:
        return None
    for index, (codes, expected) in enumerate(zip(found.codes, golden.codes)):
        for position, name in enumerate(COMPONENTS):
            span = slice(position * CODE, (position + 1) * CODE)
            if codes[span] != expected[span]:
                return Difference(run.sample, run.label, index, found.described[index], name)
    if len(found.codes) != golden.entries:
        index = min(len(found.codes), golden.entries)
        where = found.described[index] if index < len(found.described) else "<past the end>"
        return Difference(run.sample, run.label, index, where, "entries")
    return Difference(run.sample, run.label, None, "every code equal", "the last links")


@dataclass
class Replayed:
    runs: int = 0
    inputs: int = 0
    differences: list[Difference] = field(default_factory=list)
    seconds: float = 0.0


def replay(
    factory: OracleFactory,
    samples: Sequence[str] = SAMPLES,
    *,
    path: Path = GOLDENS,
    limit: int | None = None,
    stop_at_first: bool = False,
) -> Replayed:
    """Replay `factory`'s oracle over the samples and compare each run with
    its golden. `limit` keeps the first runs of each sample only."""
    started = time.perf_counter()
    goldens: dict[str, list[Golden]] = {}
    for golden in read_goldens(path):
        goldens.setdefault(golden.sample, []).append(golden)
    summary = Replayed()
    for sample in samples:
        expected = goldens.get(sample, [])
        ran = 0
        for ordinal, run in enumerate(sample_runs(sample)):
            if limit is not None and ordinal >= limit:
                break
            if ordinal >= len(expected):
                summary.differences.append(
                    Difference(sample, run.label, None, "no golden for this run", "run")
                )
                break
            found = digest_run(run, factory)
            ran += 1
            summary.runs += 1
            summary.inputs += len(found.codes)
            difference = compare(run, expected[ordinal], found)
            if difference is not None:
                summary.differences.append(difference)
                if stop_at_first:
                    break
        else:
            if ran < len(expected):
                summary.differences.append(
                    Difference(sample, expected[ran].label, None, "a golden with no run", "run")
                )
        if stop_at_first and summary.differences:
            break
    summary.seconds = time.perf_counter() - started
    return summary


def factory_named(value: str) -> OracleFactory:
    module, sep, name = value.partition(":")
    if not (sep and module and name):
        raise ValueError(f"--factory {value!r}: expected MODULE:NAME")
    factory: OracleFactory = getattr(importlib.import_module(module), name)
    return factory


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("record", "replay"))
    parser.add_argument(
        "--factory", help="the oracle's factory, MODULE:NAME (default: the engine's oracle)"
    )
    parser.add_argument("--sample", action="append", choices=SAMPLES, help="replay one sample")
    parser.add_argument("--limit", type=int, help="replay the first runs of each sample only")
    args = parser.parse_args(argv)
    factory: OracleFactory = factory_named(args.factory) if args.factory else PRODUCTION
    if args.command == "record":
        recorded = record(factory)
        print(f"{recorded.runs} runs, {recorded.inputs} entries; {recorded.seconds:.1f} s")
        for key, count in recorded.violations.items():
            if count:
                print(f"violations: {key}: {count} inputs")
        return 0
    replayed = replay(factory, args.sample or SAMPLES, limit=args.limit)
    print(
        f"{replayed.runs} runs, {replayed.inputs} entries, {len(replayed.differences)}"
        f" differing; {replayed.seconds:.1f} s"
    )
    for difference in replayed.differences[:20]:
        print(difference.report())
    return 1 if replayed.differences else 0


if __name__ == "__main__":
    sys.exit(main())
