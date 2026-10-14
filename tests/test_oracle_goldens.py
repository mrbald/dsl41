"""The oracle reproduces its golden digests (DL-351).

The default suite replays the corpus and the directed estates of the
`corpus-seeds` sample; CI replays the whole sample as its own job
(`uv run python tests/oracle_goldens.py replay`). A one-line change to the
oracle's trace is caught at the first input it reaches, named by run, input
and component.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from oracle_goldens import (
    CODE,
    COMPONENTS,
    GOLDENS,
    PRODUCTION,
    SAMPLES,
    OracleFactory,
    main,
    read_goldens,
    replay,
)
from oracle_lockstep import corpus_jobs, directed_jobs

import dsl41.oracle_chart
from dsl41.ir import lower_source
from dsl41.oracle_chart import ChartOracle
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.state_machine import Transition, Violation

#: The oracle the goldens hold to: the engine's.
ORACLE: OracleFactory = ChartOracle


def test_the_replay_holds_the_engine_s_oracle_to_the_goldens() -> None:
    engine = Engine(
        lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"),
        clock=VirtualClock(start=datetime(2026, 7, 1, 8, 0)),
        adapters={"CMD": FakeAdapter()},
    )
    assert type(engine.oracle) is PRODUCTION is ORACLE


def _directed_runs() -> int:
    return len(corpus_jobs()) + len(directed_jobs())


def test_the_oracle_reproduces_the_goldens_of_the_corpus_and_the_directed_estates() -> None:
    replayed = replay(ORACLE, ["corpus-seeds"], limit=_directed_runs())
    assert [d.report() for d in replayed.differences] == []
    assert replayed.runs == _directed_runs()


def test_the_golden_file_covers_every_sample_and_stays_small() -> None:
    goldens = read_goldens()
    assert {golden.sample for golden in goldens} == set(SAMPLES)
    width = CODE * len(COMPONENTS)
    assert all(len(code) == width for golden in goldens for code in golden.codes)
    assert all(len(golden.codes) == golden.entries for golden in goldens)
    assert GOLDENS.stat().st_size < 1_100_000


def _planted(old: str, new: str) -> OracleFactory:
    """The oracle of a copy of its module with one line changed."""
    path = Path(dsl41.oracle_chart.__file__)
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == 1
    module = types.ModuleType("dsl41_oracle_planted")
    sys.modules[module.__name__] = module
    try:
        exec(compile(source.replace(old, new), "<planted oracle>", "exec"), module.__dict__)
    finally:
        del sys.modules[module.__name__]
    factory: OracleFactory = getattr(module, ORACLE.__name__)
    return factory


def test_a_planted_one_line_change_fails_the_replay_naming_its_first_entry() -> None:
    planted = _planted('"term_run_time exceeded (dossier ss5)"', '"term_run_time exceeded"')
    replayed = replay(planted, ["corpus-seeds"], limit=_directed_runs(), stop_at_first=True)
    assert len(replayed.differences) == 1
    found = replayed.differences[0]
    # the first term_run_time of the sample fires on an advance of the clock
    assert (found.label, found.index, found.component) == (
        "kitchen_sink.jil script seed 0; switches defaults",
        18,
        "trace",
    )
    assert found.input == "2026-03-08T04:56:00 ADVANCE"
    assert found.report().splitlines() == [
        f"golden difference in corpus-seeds: {found.label}",
        "  at entry 18: 2026-03-08T04:56:00 ADVANCE",
        "  component: trace",
    ]


def test_the_command_line_replays_one_sample(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["replay", "--sample", "large", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("1 runs, ")
    assert " entries, 0 differing; " in out


class _Violating(ChartOracle):
    """Writes every status as the oracle does, and notes a violation of the
    declared tables on each: the trace and the store are the goldens'."""

    def _write(
        self,
        job: str,
        move: Transition[Any],
        cause: str,
        *,
        code: int | None = None,
        clear: bool = False,
    ) -> None:
        super()._write(job, move, cause, code=code, clear=clear)
        self.store.note_violation(
            job, Violation("job_status", "job_status.99", "STARTING", "RUNNING", "planted")
        )


def test_a_run_that_records_a_violation_fails_the_replay() -> None:
    """The violation channel is not digested, and the goldens recorded none:
    a run whose inputs record one differs at the first such entry."""
    replayed = replay(_Violating, ["corpus-seeds"], limit=5, stop_at_first=True)
    assert len(replayed.differences) == 1
    found = replayed.differences[0]
    assert found.component == "violations"
    assert found.index is not None and found.index > 0
