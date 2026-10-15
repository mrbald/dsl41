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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from estate_gen import Advance, SealPoint
from oracle_goldens import (
    CODE,
    COMPONENTS,
    GOLDENS,
    PRODUCTION,
    SAMPLES,
    Golden,
    OracleFactory,
    Run,
    compare,
    digest_run,
    label_digest,
    main,
    read_goldens,
    replay,
)
from oracle_inputs import Job, corpus_jobs, directed_jobs, observations

import dsl41.oracle
import dsl41.oracle_writers
from dsl41.ir import lower_source
from dsl41.oracle import InputBatch, Oracle
from dsl41.oracle_state import CascadeDepthError, Event, OracleError
from dsl41.semantics import resolve
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock

#: The oracle the goldens hold to: the engine's.
ORACLE: OracleFactory = Oracle


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


def _copy(module: types.ModuleType, old: str = "", new: str = "") -> types.ModuleType:
    """A copy of `module` with one line changed, built under its own name, so
    a module copied after it imports this copy."""
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    assert source.count(old) == 1
    copy = types.ModuleType(module.__name__)
    exec(compile(source.replace(old, new), f"<planted {module.__name__}>", "exec"), copy.__dict__)
    sys.modules[module.__name__] = copy
    return copy


def _planted(old: str, new: str) -> OracleFactory:
    """The oracle of a copy of its writers' module with one line changed: the
    writers' copy, and a copy of `oracle` built on it."""
    saved = {name: sys.modules[name] for name in ("dsl41.oracle_writers", "dsl41.oracle")}
    try:
        _copy(dsl41.oracle_writers, old, new)
        module = _copy(dsl41.oracle, "class Oracle(OracleWriters):", "class Oracle(OracleWriters):")
    finally:
        sys.modules.update(saved)
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


# ------------------------------------------------------------- one run's digests


def _run(jil: str, script: list[Any], **switches: str) -> Run:
    return Run(
        "directed", "directed run", "defaults", lower_source(jil), tuple(script), resolve(switches)
    )


def _difference(run: Run, factory: OracleFactory, reference: OracleFactory = ORACLE) -> Any:
    """Where `factory`'s digests of the run leave `reference`'s, if anywhere."""
    expected = digest_run(run, reference)
    golden = Golden(
        run.sample,
        label_digest(run.label),
        len(expected.codes),
        expected.last(),
        tuple(expected.codes),
    )
    return compare(run, golden, digest_run(run, factory))


#: A box and its member, and a job whose postponed start's timer falls due at
#: 10:00. The STATUS on the member raises where an oracle plants it
#: (`_RaisesOnStatus`).
_TIMER_ESTATE = (
    "insert_job: tb\njob_type: b\n\n"
    "insert_job: tm\njob_type: c\ncommand: x\nmachine: m1\nbox_name: tb\n"
    "box_terminator: 1\njob_terminator: 1\n\n"
    "insert_job: wd\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\nrun_window: "10:00-11:00"\n'
)
_T0 = datetime(2026, 7, 1, 8, 0)
#: the raising STATUS at 10:00, after a postponed start whose timer falls due
#: then, and one more input after it
_LOOP_SCRIPT = [
    Event(at=_T0, kind="FORCE_STARTJOB", payload={"job": "tb"}),
    Event(at=_T0 + timedelta(minutes=1), kind="STARTJOB", payload={"job": "wd"}),
    Event(at=_T0 + timedelta(hours=2), kind="STATUS", payload={"job": "tm", "exit_code": 2}),
    Event(at=_T0 + timedelta(hours=2, minutes=1), kind="STARTJOB", payload={"job": "tb"}),
]


class _RaisesOnStatus(Oracle):
    """An oracle that raises on every STATUS: a cascade error."""

    def _handle_status(self, ev: Event) -> None:
        raise CascadeDepthError("tm", "STATUS", 0)


class _NoTimers(_RaisesOnStatus):
    """An oracle that raises on every STATUS and whose time half fires no timer."""

    def _fire_timers_due(self, at: datetime) -> None:
        return None


def test_an_input_that_raises_is_put_back_and_its_time_half_applied_alone() -> None:
    """The engine commits none of a raising input's partial writes: it
    quarantines an engine-made input, its time half committed, and goes on.
    The digests apply the time half alone, here the postponed start's timer,
    and the script goes on to its seal; a time half that fires no timer
    parts there."""
    found = digest_run(_run(_TIMER_ESTATE, _LOOP_SCRIPT), _RaisesOnStatus)
    assert len(found.codes) == 6  # the genesis, four inputs, the end's seal
    assert found.described[3].endswith("(raised CascadeDepthError; its time half alone)")
    difference = _difference(_run(_TIMER_ESTATE, _LOOP_SCRIPT), _NoTimers, _RaisesOnStatus)
    assert difference is not None and (difference.index, difference.component) == (3, "trace")


class _TimeHalfFault(Oracle):
    """An oracle whose time half raises from 10:00 on."""

    def _fire_timers_due(self, at: datetime) -> None:
        if at >= _T0 + timedelta(hours=2):
            raise OracleError("planted time-half fault")
        super()._fire_timers_due(at)


def test_a_time_half_that_raises_stops_the_run_as_it_stops_the_engine() -> None:
    """When an input raises and its time half, applied alone, raises too, the
    engine stops there (`TimeHalfStop`), and so do the digests: no later
    input is applied, and the end's seal follows."""
    found = digest_run(_run(_TIMER_ESTATE, _LOOP_SCRIPT), _TimeHalfFault)
    assert len(found.codes) == 5  # the genesis, three inputs, the end's seal
    assert found.described[3].endswith("(raised OracleError; its time half alone)")


_TWO_JOBS = (
    "insert_job: alpha\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: beta\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(alpha)\n"
)


class _CatalogWriter(Oracle):
    """An oracle that rewrites a job's IR as it is built."""

    def __init__(self, catalog: Any, **kwargs: Any) -> None:
        catalog.jobs["alpha"].sem.max_exit_success = 3
        super().__init__(catalog, **kwargs)


def test_each_run_builds_its_oracle_on_its_own_copy_of_the_catalog() -> None:
    """The IR is mutable and read lazily: an oracle that writes its catalog
    reaches neither the run's catalog nor a later run, and its own trace
    shows the write."""
    script = [
        Event(at=_T0, kind="STARTJOB", payload={"job": "alpha"}),
        Event(
            at=_T0 + timedelta(minutes=1), kind="STATUS", payload={"job": "alpha", "exit_code": 2}
        ),
    ]
    run = _run(_TWO_JOBS, script)
    difference = _difference(run, _CatalogWriter)
    assert difference is not None and (difference.index, difference.component) == (2, "trace")
    assert run.catalog.jobs["alpha"].sem.max_exit_success != 3
    assert _difference(run, ORACLE) is None


class _TimerEditBatch(InputBatch):
    def __exit__(self, *exc: object) -> None:
        super().__exit__(*exc)
        oracle = self._oracle
        assert isinstance(oracle, _TimerEditor)
        oracle.inputs += 1
        if oracle.inputs == 2:  # after the input that armed it
            for _due, _token, timer in oracle.store.timers():
                timer.payload["note"] = "edited in place"


class _TimerEditor(Oracle):
    """An oracle that edits its armed timers' events in place at its second
    input, one after the input that armed them: a render cached since the
    arming input would not see the edit."""

    inputs = 0

    def batch(self, at: datetime) -> InputBatch:
        return _TimerEditBatch(self, at)


def test_an_in_place_edit_of_an_armed_timer_differs_at_its_input() -> None:
    script = [
        Event(at=_T0, kind="STARTJOB", payload={"job": "gamma"}),
        Advance(_T0 + timedelta(minutes=1)),
        SealPoint(),
    ]
    run = _run(
        "insert_job: gamma\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 30\n", script
    )
    difference = _difference(run, _TimerEditor)
    assert difference is not None and (difference.index, difference.component) == (2, "store")


class _NoWakeUp(Oracle):
    """An oracle whose wake-up read misses every timer."""

    def next_timer_due(self) -> datetime | None:
        return None


def test_the_wake_up_read_is_digested() -> None:
    script = [Event(at=_T0, kind="STARTJOB", payload={"job": "gamma"})]
    run = _run(
        "insert_job: gamma\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 30\n", script
    )
    difference = _difference(run, _NoWakeUp)
    assert difference is not None and (difference.index, difference.component) == (
        1,
        "next_timer_due",
    )


@pytest.mark.parametrize("recheck", ["0", "1", "2"])
def test_the_directed_estate_makes_the_wekr_rows_live(recheck: str) -> None:
    """The switch as the mutant: the oracle under `partial` and under
    `first-full`. The estate tells them apart wherever `queued-recheck` reads
    a calendar, so an oracle that ignored the switch would fail the switch
    matrix's pair rows."""
    [job] = [job for job in directed_jobs() if job.label == "<directed wekr>"]
    partial = {"wekr-first-week": "partial", "queued-recheck": recheck}
    full = {"wekr-first-week": "first-full", "queued-recheck": recheck}
    run = Run("switches", job.label, "pair", job.catalog, job.script, resolve(partial))
    found = digest_run(
        Run("switches", job.label, "pair", job.catalog, job.script, resolve(full)), ORACLE
    )
    golden = digest_run(run, ORACLE)
    expected = Golden(
        run.sample, label_digest(run.label), len(golden.codes), golden.last(), tuple(golden.codes)
    )
    difference = compare(run, expected, found)
    if recheck == "0":
        assert difference is None
        return
    assert difference is not None, recheck
    # the first release: wk_skip's exclusion holds 2014-01-06 only under partial
    assert (difference.index, difference.component) == (5, "trace")
    steps = [step for step in job.script if not isinstance(step, SealPoint)]
    seen = {
        name: observations(
            Oracle(job.catalog.model_copy(deep=True), semantics=resolve(switches)), steps
        )
        for name, switches in (("partial", partial), ("full", full))
    }
    first = next(i for i, (a, b) in enumerate(zip(seen["partial"], seen["full"])) if a != b)
    assert any(
        "wk_skip" in line for line in seen["partial"][first]["trace"] + seen["full"][first]["trace"]
    )


def test_a_jobs_own_switch_fills_only_what_the_matrix_row_leaves_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directed estate's switch (`Job.switches`) fills a switch the row
    leaves out; a row that sets it wins, so the estate runs under every value
    the matrix names, and a merged row it has run under already runs once."""
    import oracle_goldens

    pinned = next(j for j in directed_jobs() if j.switches)
    plain = next(j for j in directed_jobs() if "kill and timer" in j.label and not j.switches)
    matrix = [{}, {"queued-recheck": "1"}, {"queued-recheck": "2"}, {"queued-recheck": "0"}]
    monkeypatch.setattr(oracle_goldens, "sample_jobs", lambda _name: ([pinned, plain], matrix))
    runs = list(oracle_goldens.sample_runs("switches"))
    labels = [run.label for run in runs]
    assert len(labels) == len(set(labels))
    assert [r.semantics.queued_recheck for r in runs if r.label.startswith(pinned.label)] == [
        "1",
        "2",
        "0",
    ]
    assert [r.semantics.queued_recheck for r in runs if r.label.startswith(plain.label)] == [
        "0",
        "1",
        "2",
        "0",
    ]


def test_two_jobs_with_one_label_each_run_under_every_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dedupe of merged rows is per job, not per label: two jobs that
    share a label both run."""
    import oracle_goldens

    job = directed_jobs()[0]
    twin = Job(job.label, job.catalog, job.script[:1])
    matrix = [{}, {"queued-recheck": "2"}]
    monkeypatch.setattr(oracle_goldens, "sample_jobs", lambda _name: ([job, twin], matrix))
    assert len(list(oracle_goldens.sample_runs("switches"))) == 4
