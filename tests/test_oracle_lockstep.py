"""The old-vs-new oracle harness, proven old-vs-old (statechart-engine ss16,
DL-338).

With both sides the current `Oracle`, the corpus, generated estates and the
semantic-switch matrix give no divergence. A one-line fault in a copy of
oracle.py is caught at the first input it affects, and hypothesis shrinks a
generated counterexample to one job. The full fixed sample runs when
`DSL41_ORACLE_LOCKSTEP` is set, against the new oracle it names.
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import sys
import textwrap
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast

import pytest
from estate_gen import Advance, Case, SealPoint, Step, cases, seeded
from hypothesis import HealthCheck, find, given, settings
from oracle_lockstep import (
    FIELDS,
    LOCKSTEP_ENV,
    MUTANTS,
    Lockstep,
    LockstepDivergence,
    LockstepOracle,
    WEKR_JIL,
    corpus_jobs,
    directed_jobs,
    generated_jobs,
    main,
    mutant_factory,
    new_oracle_factory,
    run,
    run_all,
    seal_state_payload,
    sealed_state,
    switch_matrix,
)
from test_seal_artifact import _seal

from dsl41.ir import CatalogIR, lower_source
from dsl41.oracle import InputBatch, Oracle
from dsl41.oracle_state import CascadeDepthError, Event, JobRuntime, OracleError
from dsl41.runner import Engine
from dsl41.seal import SealedState
from dsl41.semantics import REGISTRY
from dsl41.semantics import resolve as resolve_switches
from dsl41.state_machine import HITS_ENV, STRICT_ENV

T0 = datetime(2026, 7, 1, 8, 0)

#: the full proof runs with the arm: the fixed sample, and the slow shrink
_FULL = pytest.mark.skipif(not os.environ.get(LOCKSTEP_ENV), reason=f"{LOCKSTEP_ENV} is not set")

#: the event kinds the generated scripts must carry (ss16's input families)
SCRIPT_KINDS = {
    "STATUS",
    "STARTJOB",
    "FORCE_STARTJOB",
    "KILLJOB",
    "ON_ICE",
    "OFF_ICE",
    "ON_HOLD",
    "OFF_HOLD",
    "ON_NOEXEC",
    "OFF_NOEXEC",
    "DISARM",
    "RELEASE_RESOURCE",
    "SET_GLOBAL",
}


@pytest.fixture
def quiet_hits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutant may break a declared transition after its divergence; its
    hits and violations stay out of the session's record."""
    monkeypatch.setenv(HITS_ENV, str(tmp_path))
    monkeypatch.delenv(STRICT_ENV, raising=False)


def _case_run(case: Case, new: Callable[..., Oracle] = Oracle):
    return run(lower_source(case.jil), case.script, new=new, label=case.label)


# ------------------------------------------------------------- old vs old


def test_old_vs_old_corpus_has_no_divergence() -> None:
    jobs = corpus_jobs()
    summary = run_all(jobs, [{}])
    assert summary.divergences == []
    # every lowerable corpus file with jobs, and the whole corpus, six scripts each
    assert summary.runs == len(jobs) >= 6 * 25
    assert summary.inputs > 3000
    assert summary.unsealable == 0


def test_old_vs_old_generated_estates_have_no_divergence() -> None:
    summary = run_all(generated_jobs(range(12), 30), [{}])
    assert summary.divergences == []
    assert summary.inputs > 500
    assert summary.seals > 12


def test_old_vs_old_under_every_switch_value() -> None:
    jobs = generated_jobs(range(3), 20) + directed_jobs()
    jobs += [job for job in corpus_jobs(scripts=1) if job.label.startswith("<whole corpus>")]
    summary = run_all(jobs, switch_matrix())
    assert summary.divergences == []
    assert summary.runs == len(jobs) * len(switch_matrix())


@given(cases(max_size=8))
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_old_vs_old_on_hypothesis_cases(case: Case) -> None:
    assert _case_run(case).divergence is None


@_FULL
def test_the_fixed_sample() -> None:
    """The proof the harness gives: the corpus, 200 estates of up to 50
    jobs and 20 of 150 to 200 at the defaults, and the corpus and 40
    estates under every switch value."""
    new = new_oracle_factory()
    assert new is not None
    small = generated_jobs(range(200), 50)
    large = generated_jobs(range(1000, 1020), 200, min_size=150)
    matrix = corpus_jobs() + directed_jobs() + generated_jobs(range(2000, 2040), 50)
    found = run_all(small + large, [{}], new=new).divergences
    found += run_all(matrix, switch_matrix(), new=new).divergences
    assert found == [], found[0].report()


# ------------------------------------------------------------- the inputs


def test_the_generator_is_deterministic_per_seed() -> None:
    assert seeded(7, 50) == seeded(7, 50)
    assert seeded(7, 50) != seeded(8, 50)


def test_a_seeded_estate_has_the_jobs_it_was_sized_for() -> None:
    sizes = [seeded(seed, 200, min_size=150).jobs for seed in range(10)]
    assert all(150 <= size <= 200 for size in sizes)
    assert all(1 <= seeded(seed, 5).jobs <= 5 for seed in range(30))


def test_the_generated_sample_carries_every_family() -> None:
    sample = [seeded(seed, 50) for seed in range(30)]
    kinds = {step.kind for case in sample for step in case.script if isinstance(step, Event)}
    assert kinds == SCRIPT_KINDS
    assert any(isinstance(step, Advance) for case in sample for step in case.script)
    assert any(isinstance(step, SealPoint) for case in sample for step in case.script)
    text = "\n".join(case.jil for case in sample)
    for attribute in (
        "job_type: b",
        "box_name:",
        "auto_hold: 1",
        "start_mins:",
        "box_success:",
        "box_failure:",
        "box_terminator: 1",
        "job_terminator: 1",
        "run_window:",
        "run_calendar:",
        "exclude_calendar:",
        "extended_calendar:",
        "calendar:",
        "timezone:",
        "must_start_times:",
        "must_complete_times:",
        "term_run_time:",
        "resources:",
        "max_load:",
        "job_load:",
        "insert_global:",
        "status: ON_ICE",
        "status: ON_HOLD",
        "status: ON_NOEXEC",
        "res_type: R",
        "res_type: D",
        "res_type: T",
        "FREE=",
    ):
        assert attribute in text, attribute
    # a condition on the job itself or on a later one: the loops
    forward = 0
    for block in text.split("\n\n"):
        own = re.search(r"insert_job: \w+_(\d+)", block)
        named = re.search(r"^condition: (.*)$", block, re.MULTILINE)
        if own and named:
            cited = re.findall(r"\b(?:job|box)_(\d+)", named.group(1))
            forward += any(int(index) >= int(own.group(1)) for index in cited)
    assert forward > 0
    for atom in ("s(", "f(", "d(", "n(", "t(", "e(", "value("):
        assert f"condition: {atom}" in text or f" {atom}" in text, atom


def test_the_switch_matrix_is_each_value_of_each_switch_once() -> None:
    matrix = switch_matrix()
    assert matrix[0] == {}
    expected = [
        (name, value)
        for name, switch in REGISTRY.items()
        for value in switch.values
        if value != switch.default
    ]
    singles = matrix[1 : 1 + len(expected)]
    assert [next(iter(entry.items())) for entry in singles] == expected
    assert all(len(entry) == 1 for entry in singles)
    # the named pairs: the wekr switch under each queued-recheck value that reads it
    assert matrix[1 + len(expected) :] == [
        {"wekr-first-week": "partial", "queued-recheck": "1"},
        {"wekr-first-week": "partial", "queued-recheck": "2"},
    ]


# ------------------------------------------------------------- the mutants


def _first_affected(case: Case, affects: Callable[[Oracle, Step], bool]) -> int | None:
    """The 1-based index of the first input of `case` the mutant changes,
    read off the unmutated oracle."""
    oracle = Oracle(lower_source(case.jil))
    for index, step in enumerate(case.script, start=1):
        if isinstance(step, SealPoint):
            continue
        if affects(oracle, step):
            return index
        try:
            if isinstance(step, Event):
                oracle.feed(step)
            else:
                oracle.advance(step.at)
        except OracleError:
            pass
    return None


def _injects_a_status(oracle: Oracle, step: Step) -> bool:
    """`_handle_status` moves the job with the cause the `cause` mutant changes."""
    return (
        isinstance(step, Event)
        and step.kind == "STATUS"
        and step.payload.get("status") != "INACTIVE"
    )


def _clears_a_hold(oracle: Oracle, step: Step) -> bool:
    """An OFF_HOLD on a held job: the move the `hold-clear` mutant drops."""
    if not isinstance(step, Event) or step.kind != "OFF_HOLD":
        return False
    return oracle.store.job[str(step.payload["job"])].on_hold


AFFECTS = {"cause": _injects_a_status, "hold-clear": _clears_a_hold}


def test_every_mutant_has_its_first_input_rule() -> None:
    assert set(AFFECTS) == set(MUTANTS)


@pytest.mark.parametrize("name", sorted(MUTANTS))
@pytest.mark.usefixtures("quiet_hits")
def test_a_mutant_is_caught_at_the_first_input_it_affects(name: str) -> None:
    mutant = mutant_factory(name)
    caught = 0
    for seed in range(100):
        case = seeded(seed, 30)
        expected = _first_affected(case, AFFECTS[name])
        found = _case_run(case, mutant).divergence
        if expected is None:
            assert found is None, found.report()
            continue
        assert found is not None, case.label
        assert found.index == expected, found.report()
        caught += 1
    assert caught >= 5


#: `hold-clear` needs a held job and an OFF_HOLD on it, which a random
#: estate rarely has: its search and shrink take about a minute, so it runs
#: with the full sample only
_SHRINKS = ["cause", pytest.param("hold-clear", marks=_FULL)]


@pytest.mark.parametrize("name", _SHRINKS)
@pytest.mark.usefixtures("quiet_hits")
def test_hypothesis_shrinks_a_mutant_to_one_job(name: str) -> None:
    mutant = mutant_factory(name)
    smallest = find(
        cases(max_size=8),
        lambda case: _case_run(case, mutant).divergence is not None,
        settings=settings(max_examples=2000, database=None, derandomize=True),
    )
    assert smallest.jobs == 1
    assert len(smallest.script) <= 2


_TWO_JOBS = (
    "insert_job: alpha\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: beta\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(alpha)\n"
)


@pytest.mark.usefixtures("quiet_hits")
def test_the_report_names_the_input_the_field_and_both_values() -> None:
    script = [
        Event(at=T0, kind="ON_HOLD", payload={"job": "beta"}),
        SealPoint(),
        Event(
            at=T0 + timedelta(minutes=1),
            kind="STATUS",
            payload={"job": "alpha", "status": "SUCCESS"},
        ),
    ]
    found = run(lower_source(_TWO_JOBS), script, new=mutant_factory("cause"), label="two jobs")
    assert found.divergence is not None
    assert found.divergence.report() == "\n".join(
        [
            "lockstep divergence at input 3 (two jobs)",
            "input: 2026-07-01T08:01:00 STATUS job=alpha status=SUCCESS",
            "field: trace",
            "--- old",
            "+++ new",
            "@@ -1 +1 @@",
            "-2026-07-01T08:01:00 alpha INACTIVE->SUCCESS [injected STATUS]",
            "+2026-07-01T08:01:00 alpha INACTIVE->SUCCESS [injected status]",
        ]
    )


@pytest.mark.usefixtures("quiet_hits")
def test_a_dropped_flag_clear_names_every_field_it_moves() -> None:
    script = [
        Event(at=T0, kind="ON_HOLD", payload={"job": "beta"}),
        Event(at=T0, kind="STATUS", payload={"job": "alpha", "status": "SUCCESS"}),
        Event(at=T0 + timedelta(minutes=1), kind="OFF_HOLD", payload={"job": "beta"}),
    ]
    found = run(lower_source(_TWO_JOBS), script, new=mutant_factory("hold-clear")).divergence
    assert found is not None
    assert (found.index, found.field) == (3, "trace")
    assert found.also == ("emitted", "store", "revisions")


def test_a_mutant_line_must_occur_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(MUTANTS, "absent", ("no such line\n", ""))
    with pytest.raises(ValueError, match="occurs 0 times"):
        mutant_factory("absent")


# ------------------------------------------------------------- the seal


def test_the_seal_state_section_is_serialized_as_the_seal_writes_it() -> None:
    seal = _seal()
    assert seal_state_payload(seal.state) == seal.to_payload()["state"]


def test_a_difference_only_the_seal_shows_is_reported() -> None:
    catalog = lower_source(_TWO_JOBS)
    pair = Lockstep(Oracle(catalog), Oracle(catalog))
    assert pair.genesis() is None
    assert pair.step(Event(at=T0, kind="STARTJOB", payload={"job": "alpha"}))[1] is None
    assert pair.seal() is None
    # a write outside any input: no per-input comparison sees it
    store = pair.new.oracle.store
    store._jobs["alpha"] = store.job["alpha"].model_copy(update={"run_number": 9})
    found = pair.seal()
    assert found is not None
    assert (found.index, found.field) == (1, "seal")
    assert found.old[0].startswith("digest sha256:")
    assert found.old[0] != found.new[0]


def test_an_unsealable_state_is_compared_as_its_refusal() -> None:
    catalog = lower_source(_TWO_JOBS)
    pair = Lockstep(Oracle(catalog), Oracle(catalog))
    for side in (pair.old, pair.new):
        side.oracle.store._enqueue_counter = -1
    assert pair.seal() is None
    assert pair.unsealable
    assert run(catalog, []).unsealable == 0
    pair.old.oracle.store._enqueue_counter = 0
    found = pair.seal()
    assert found is not None
    assert found.new[0].startswith("digest unsealable: ValidationError")


# ------------------------------------------------------------- the arm


def test_the_arm_is_off_unless_the_variable_names_an_oracle() -> None:
    assert new_oracle_factory("") is None
    assert new_oracle_factory("0") is None
    assert new_oracle_factory("1") is Oracle
    assert new_oracle_factory("dsl41.oracle:Oracle") is Oracle
    for bad in ("yes", "dsl41.oracle", ":Oracle", "dsl41.oracle:"):
        with pytest.raises(ValueError, match="expected 0, 1 or MODULE:NAME"):
            new_oracle_factory(bad)


@pytest.mark.skipif(bool(os.environ.get(LOCKSTEP_ENV)), reason=f"{LOCKSTEP_ENV} is set")
def test_with_the_variable_unset_the_sem_tests_run_three_ways() -> None:
    """The suite's runtime does not grow until the new oracle is wired."""
    import test_oracle

    assert test_oracle._PATHS == ["direct", "engine", "fork"]


@pytest.mark.usefixtures("quiet_hits")
def test_the_arm_raises_at_the_first_divergence() -> None:
    paired = LockstepOracle(lower_source(_TWO_JOBS), new=mutant_factory("cause"))
    paired.feed(Event(at=T0, kind="STARTJOB", payload={"job": "beta"}))
    with pytest.raises(LockstepDivergence, match="field: trace"):
        paired.feed(Event(at=T0, kind="STATUS", payload={"job": "alpha", "status": "FAILURE"}))


def test_the_arm_reraises_an_error_both_oracles_raise() -> None:
    paired = LockstepOracle(lower_source(_TWO_JOBS))
    with pytest.raises(OracleError, match="unknown status"):
        paired.feed(Event(at=T0, kind="STATUS", payload={"job": "alpha", "status": "BOGUS"}))
    assert paired.advance(T0 + timedelta(hours=1)) == []
    paired.check_seal()
    assert paired.twin is not paired


# ------------------------------------------------------------- the command line


def test_the_command_line_runs_and_reports(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--seeds", "2", "--size", "5", "--switches", "ice-lookback=ordinary"]) == 0
    assert "2 runs" in capsys.readouterr().out


@pytest.mark.usefixtures("quiet_hits")
def test_the_command_line_shows_a_mutant(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--seeds", "5", "--size", "10", "--mutant", "cause"]) == 1
    out = capsys.readouterr().out
    assert "1 divergences" in out
    assert "lockstep divergence at input" in out


# ------------------------------------------------------------- rework round 1


def _equal_rows_iterating_differently() -> tuple[JobRuntime, JobRuntime]:
    """Two equal rows whose `ran_members` iterate in different orders: a
    pair of names that collide in a small set's table, found in this
    process (the order depends on the hash seed)."""
    names = [f"job_{i:03d}" for i in range(200)]
    for a in names:
        for b in names:
            if a < b and list(frozenset([a, b])) != list(frozenset([b, a])):
                return JobRuntime(ran_members=frozenset([a, b])), JobRuntime(
                    ran_members=frozenset([b, a])
                )
    raise AssertionError("no colliding pair among 200 names")  # pragma: no cover


def test_equal_rows_render_the_same_whatever_order_their_sets_iterate() -> None:
    first, second = _equal_rows_iterating_differently()
    assert first == second
    assert first.model_dump(mode="json") != second.model_dump(mode="json")
    catalog = lower_source(_TWO_JOBS)
    pair = Lockstep(Oracle(catalog), Oracle(catalog.model_copy(deep=True)))
    pair.old.oracle.store._jobs["alpha"] = first
    pair.new.oracle.store._jobs["alpha"] = second
    assert pair.genesis() is None
    assert pair.seal() is None


def test_rows_are_compared_by_name_as_the_seal_compares_them() -> None:
    catalog = lower_source(_TWO_JOBS)
    pair = Lockstep(Oracle(catalog), Oracle(catalog.model_copy(deep=True)))
    jobs = pair.new.oracle.store._jobs
    pair.new.oracle.store._jobs = dict(reversed(list(jobs.items())))
    assert pair.genesis() is None


class _CatalogWriter(Oracle):
    """A new oracle that rewrites a job's IR as it is built."""

    def __init__(self, catalog: CatalogIR, **kwargs: object) -> None:
        catalog.jobs["alpha"].sem.max_exit_success = 3
        super().__init__(catalog, **kwargs)  # type: ignore[arg-type]


_EXIT_TWO = [
    Event(at=T0, kind="STARTJOB", payload={"job": "alpha"}),
    Event(at=T0 + timedelta(minutes=1), kind="STATUS", payload={"job": "alpha", "exit_code": 2}),
]


def test_a_new_oracle_that_writes_to_its_catalog_is_caught() -> None:
    catalog = lower_source(_TWO_JOBS)
    found = run(catalog, _EXIT_TWO, new=_CatalogWriter).divergence
    assert found is not None
    assert (found.index, found.field) == (2, "trace")
    assert catalog.jobs["alpha"].sem.max_exit_success != 3  # the caller's catalog is untouched
    paired = LockstepOracle(lower_source(_TWO_JOBS), new=_CatalogWriter)
    paired.feed(_EXIT_TWO[0])
    with pytest.raises(LockstepDivergence, match="input 2"):
        paired.feed(_EXIT_TWO[1])


class _TimerEditBatch(InputBatch):
    def __exit__(self, *exc: object) -> None:
        super().__exit__(*exc)
        oracle = cast(_TimerEditor, self._oracle)
        oracle.inputs += 1
        if oracle.inputs == 2:  # after the input that armed it
            for _due, _token, timer in oracle.store.timers():
                timer.payload["note"] = "edited in place"


class _TimerEditor(Oracle):
    """A new oracle that edits its armed timers' events in place at its
    second input, one after the input that armed them: a render cached
    since the arming input would not see the edit."""

    inputs = 0

    def batch(self, at: datetime) -> InputBatch:
        return _TimerEditBatch(self, at)


def test_an_in_place_edit_of_an_armed_timer_is_reported_at_its_input() -> None:
    catalog = lower_source(
        "insert_job: gamma\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 30\n"
    )
    script = [
        Event(at=T0, kind="STARTJOB", payload={"job": "gamma"}),
        Advance(T0 + timedelta(minutes=1)),
        SealPoint(),
    ]
    found = run(catalog, script, new=_TimerEditor).divergence
    assert found is not None
    assert (found.index, found.field) == (2, "store")
    assert any("edited in place" in line for line in found.new)


def test_a_fork_of_the_arm_forks_both_sides() -> None:
    paired = LockstepOracle(lower_source(_TWO_JOBS))
    fork = paired.fork()
    assert isinstance(fork, LockstepOracle)
    fork.feed(Event(at=T0, kind="STARTJOB", payload={"job": "alpha"}))
    assert fork.store.job["alpha"].status == "RUNNING"
    assert fork.twin.store.job["alpha"].status == "RUNNING"
    assert paired.store.job["alpha"].status == "INACTIVE"
    assert paired.twin.store.job["alpha"].status == "INACTIVE"
    fork.check_seal()
    paired.feed(Event(at=T0, kind="STARTJOB", payload={"job": "alpha"}))
    paired.check_seal()


def _sealed_state_keywords(function: Callable[..., object]) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SealedState"
    ]
    assert len(calls) == 1, (
        f"{function.__qualname__} builds SealedState in {len(calls)} calls, not one:"
        " point this pin at the function that builds the seal's state now"
    )
    return {keyword.arg for keyword in calls[0].keywords if keyword.arg is not None}


def test_the_seal_state_fills_the_fields_the_engine_fills() -> None:
    fields = set(SealedState.model_fields)
    assert _sealed_state_keywords(Engine._snapshot) == fields
    assert _sealed_state_keywords(sealed_state) == fields


class _NoWakeUp(Oracle):
    """A new oracle whose wake-up read misses every timer."""

    def next_timer_due(self) -> datetime | None:
        return None


def test_the_wake_up_read_is_compared() -> None:
    assert "next_timer_due" in FIELDS
    catalog = lower_source(
        "insert_job: gamma\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 30\n"
    )
    script = [Event(at=T0, kind="STARTJOB", payload={"job": "gamma"})]
    found = run(catalog, script, new=_NoWakeUp).divergence
    assert found is not None
    assert (found.index, found.field) == (1, "next_timer_due")


def _under(**overrides: str) -> Callable[..., Oracle]:
    """An oracle factory that runs under `overrides`, whatever it is given."""

    def build(catalog: CatalogIR, **_given: object) -> Oracle:
        return Oracle(catalog, semantics=resolve_switches(overrides))

    return build


@pytest.mark.parametrize("recheck", ["0", "1", "2"])
def test_the_directed_estate_makes_the_wekr_rows_live(recheck: str) -> None:
    """The switch as the mutant: the old oracle under `partial` on one side
    and `first-full` on the other. The estate tells them apart wherever
    `queued-recheck` reads a calendar, so a new oracle that ignored the
    switch would fail the pair rows."""
    [job] = [job for job in directed_jobs() if job.label == "<directed wekr>"]
    found = run(
        lower_source(WEKR_JIL),
        job.script,
        old=_under(**{"wekr-first-week": "partial", "queued-recheck": recheck}),
        new=_under(**{"wekr-first-week": "first-full", "queued-recheck": recheck}),
    ).divergence
    if recheck == "0":
        assert found is None
        return
    assert found is not None, recheck
    # the first release: wk_skip's exclusion holds 2014-01-06 only under partial
    assert (found.index, found.field) == (5, "trace")
    assert "wk_skip" in found.report()


_TERMINATOR_LOOP = (
    "insert_job: tb\njob_type: b\ncondition: t(tb)\n\n"
    "insert_job: tm\njob_type: c\ncommand: x\nmachine: m1\nbox_name: tb\n"
    "box_terminator: 1\njob_terminator: 1\n\n"
    "insert_job: wd\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\nrun_window: "10:00-11:00"\n'
)


class _RaisesOnStatus(Oracle):
    """An oracle that raises on every STATUS: a cascade error, or a
    planted error of another text."""

    error: Exception = CascadeDepthError("tm", "STATUS", 0)

    def _handle_status(self, ev: Event) -> None:
        raise self.error


class _OtherText(_RaisesOnStatus):
    error = OracleError("planted")


class _NoTimers(Oracle):
    """An oracle whose time half fires no timer."""

    def _fire_timers_due(self, at: datetime) -> None:
        return None


_T0 = datetime(2026, 7, 1, 8, 0)


def _loop_script(at: datetime) -> list[Event]:
    """The terminator loop's raising STATUS at `at`, after a postponed start
    whose timer falls due at 10:00, and one more input after it."""
    return [
        Event(at=_T0, kind="FORCE_STARTJOB", payload={"job": "tb"}),
        Event(at=_T0 + timedelta(minutes=1), kind="STARTJOB", payload={"job": "wd"}),
        Event(at=at, kind="STATUS", payload={"job": "tm", "exit_code": 2}),
        Event(at=at + timedelta(minutes=1), kind="STARTJOB", payload={"job": "tb"}),
    ]


@pytest.mark.parametrize(
    ("loop", "new", "expected"),
    [
        ("t(tb)", Oracle, None),
        ("t(tb)", _NoTimers, "STATUS job=tm exit_code=2 (both raised CascadeDepthError"),
        ("s(tb)", _RaisesOnStatus, "outcome"),
        ("t(tb)", _OtherText, "outcome"),
    ],
    ids=["alike", "time half differs", "one side", "different errors"],
)
def test_an_input_both_sides_raise_alike_compares_its_time_half_and_goes_on(
    loop: str, new: Callable[..., Oracle], expected: str | None
) -> None:
    """Both sides raising alike agree on the input's attempt half, whose
    partial writes the engine never commits. Its time half, which a
    quarantined engine-made input commits, is applied alone on both and
    compared, here the postponed start's timer, and the script goes on to its
    seal. One side raising, or the two raising differently, is a divergence
    of the outcome that names both errors."""
    limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(1000)
        result = run(
            lower_source(_TERMINATOR_LOOP.replace("t(tb)", loop)),
            _loop_script(_T0 + timedelta(hours=2)),
            new=new,
        )
    finally:
        sys.setrecursionlimit(limit)
    if expected is None:
        assert result.divergence is None
        assert (result.raised_alike, result.inputs, result.seals) == (1, 4, 1)
        return
    found = result.divergence
    assert found is not None and found.index == 3
    if expected == "outcome":
        assert found.field == "outcome" and result.raised_alike == 0
        assert found.old != found.new and (found.old or found.new)
    else:
        assert expected in found.input and found.field == "trace"


class _TimeHalfFault(Oracle):
    """An oracle whose time half raises from 10:00 on."""

    def _fire_timers_due(self, at: datetime) -> None:
        if at >= _T0 + timedelta(hours=2):
            raise OracleError("planted time-half fault")
        super()._fire_timers_due(at)


class _RetriedTimeHalfFault(Oracle):
    """An oracle whose time half raises only when applied alone, after the
    input's attempt half raised: the second application at one instant."""

    applied: ClassVar[list[datetime]] = []

    def _fire_timers_due(self, at: datetime) -> None:
        if at >= _T0 + timedelta(hours=2):
            self.applied.append(at)
            if self.applied.count(at) > 1:
                raise OracleError("planted time-half fault")
        super()._fire_timers_due(at)


@pytest.mark.parametrize(
    ("old", "new"),
    [(_TimeHalfFault, _TimeHalfFault), (Oracle, _RetriedTimeHalfFault)],
    ids=["alike", "one side"],
)
def test_a_time_half_that_raises_alike_stops_the_pair_as_it_stops_the_engine(
    old: Callable[..., Oracle], new: Callable[..., Oracle]
) -> None:
    """When an input raises alike on both sides and its time half, applied
    alone, raises alike too, the engine stops there (`TimeHalfStop`), and so
    does the pair: no seal, no divergence. A time half that raises on one
    side only is a divergence of the outcome that names both."""
    _RetriedTimeHalfFault.applied = []
    limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(1000)
        result = run(
            lower_source(_TERMINATOR_LOOP), _loop_script(_T0 + timedelta(hours=2)), old=old, new=new
        )
    finally:
        sys.setrecursionlimit(limit)
    if new is _TimeHalfFault:
        assert result.divergence is None
        assert (result.raised_alike, result.time_half_stopped) == (1, 1)
        assert (result.inputs, result.seals) == (3, 0)
        return
    found = result.divergence
    assert found is not None and found.index == 3 and found.field == "outcome"
    assert "its time half alone" in found.input
    assert found.new == ("OracleError: planted time-half fault",) and found.old == ()
