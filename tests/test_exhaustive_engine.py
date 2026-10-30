"""Every event in every built state, for the engine tier's machines and the
job's flags, holding and assembly (DL-289, DL-293, DL-295).

Transition coverage proves that each declared row is taken somewhere. It
does not prove that a chart is complete: a cell the suite never builds
would go unseen until production, where a cell with no row is a
`StatechartError` that stops the tier. So each test here builds the states
its machine's code reaches, applies every event that code accepts in each
of them, and records into its own directory. An event either takes a
declared row or is refused by the code's own rule. Each docstring says what
its builder leaves out.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dsl41.ir import lower_source
from dsl41.job_chart import JOB
from dsl41.oracle import Oracle
from dsl41.oracle_state import (
    LIVE,
    CapacityReservation,
    CarriedRows,
    Event,
    JobRuntime,
    OracleError,
    RuntimeState,
)
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    ADMISSION,
    ENGINE_REQUEST_ID_PREFIX,
    PROTOCOL_VERSION,
    ApplyResult,
    Attempt,
    Envelope,
    EnvelopeError,
    fingerprint,
    parse_envelope,
)
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_control import command
from dsl41.runner_store import MemoryStore
from dsl41.runner_journal import SUBSCRIPTION, Journal, read_decisions, read_journal
from dsl41.semantics import DEFAULTS, SemanticSwitches
from dsl41.semantics import resolve as resolve_switches
from dsl41.state_machine import (
    HITS_ENV,
    Executor,
    Outcome,
    StatechartError,
)
from dsl41.state_machine import Event as ChartEvent

T0 = datetime(2026, 7, 1, 8, 0)
DAY = timedelta(days=1)


def _recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, forward: bool) -> Iterator[Path]:
    """A private record directory, so a row a test takes by a verb order no
    production path makes stays out of the session's gate.

    With `forward`, the hits are handed to the session's directory
    afterwards, when there is one, so the gate sees the states built here.
    Forward only from a test whose cells take declared rows the production
    code can take."""
    session = os.environ.get(HITS_ENV)
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    yield directory
    hits = [
        line for path in directory.glob("hits-*.jsonl") for line in path.read_text().splitlines()
    ]
    if forward and session and hits:
        with open(Path(session) / f"hits-{os.getpid()}-exhaustive.jsonl", "a") as out:
            out.write("\n".join(hits) + "\n")


@pytest.fixture
def recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """`_recording`, with the hits forwarded."""
    yield from _recording(tmp_path, monkeypatch, forward=True)


@pytest.fixture
def recorded_privately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """`_recording`, with the hits kept private: the cells build states by
    verb orders no production path makes."""
    yield from _recording(tmp_path, monkeypatch, forward=False)


def _hits(directory: Path) -> set[str]:
    """The row ids taken and recorded into `directory` (a statechart's guard
    and branch records carry ids too, and are not rows taken)."""
    return {
        record["id"]
        for path in directory.glob("hits-*.jsonl")
        for record in map(json.loads, path.read_text().splitlines())
        if record["kind"] == "row"
    }


# ------------------------------------------------- the job's flags and holding

_SCHEDULED = 'date_conditions: 1\ndays_of_week: all\nstart_times: "23:00"\ncondition: s(fx)\n'
_JOB_JIL = (
    "insert_resource: XR\nres_type: R\namount: 1\n\n"
    "insert_resource: XA\nres_type: R\namount: 1\n\n"
    "insert_resource: XD\nres_type: D\namount: 100\n\n"
    "insert_job: fx\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: xh\njob_type: c\ncommand: h\nmachine: m1\n"
    "resources: (XR, QUANTITY=1, FREE=Y)\n\n"
    "insert_job: fj\njob_type: c\ncommand: j\nmachine: m1\n"
    "resources: (XR, QUANTITY=1, FREE=N)\n" + _SCHEDULED + "\n"
    "insert_job: fa\njob_type: c\ncommand: a\nmachine: m1\n"
    "resources: (XA, QUANTITY=1, FREE=A)\n" + _SCHEDULED + "\n"
    "insert_job: fd\njob_type: c\ncommand: d\nmachine: m1\n"
    "resources: (XD, QUANTITY=1)\n" + _SCHEDULED + "\n"
    "insert_job: fb\njob_type: b\n\n"
    "insert_job: fbm\njob_type: c\ncommand: m\nmachine: m1\nbox_name: fb\nauto_hold: 1\n"
    "box_terminator: 1\n" + _SCHEDULED
)
#: `box-start-all-members-out=wait`: a box started with every member iced
#: stays RUNNING, as a box start did before the switch
_WAIT = resolve_switches({"box-start-all-members-out": "wait"})
_STATUSES = ("INACTIVE", "QUE_WAIT", "STARTING", "RUNNING", "SUCCESS", "FAILURE", "TERMINATED")
#: every operator verb the oracle dispatches for a job, and an injected
#: status of each kind (a completion, or CHANGE_STATUS)
_JOB_EVENTS: tuple[tuple[str, dict[str, str]], ...] = (
    *(
        (verb, {})
        for verb in (
            "KILLJOB",
            "STARTJOB",
            "FORCE_STARTJOB",
            "ON_ICE",
            "OFF_ICE",
            "ON_HOLD",
            "OFF_HOLD",
            "ON_NOEXEC",
            "OFF_NOEXEC",
            "DISARM",
            "RELEASE_RESOURCE",
        )
    ),
    *(("STATUS", {"status": status}) for status in _STATUSES),
)
type _Step = tuple[str, str, dict[str, str]]


def _status_recipes(job: str) -> dict[str, tuple[tuple[_Step, ...], ...]]:
    """The inputs that try to put `job` in each status. The oracle runs a
    start straight to RUNNING, so STARTING comes from an injected status,
    over an idle row or over a run that holds its units."""
    force: _Step = ("FORCE_STARTJOB", job, {})
    return {
        "INACTIVE": ((), (("STATUS", job, {"status": "INACTIVE"}),)),
        "QUE_WAIT": ((("STARTJOB", "xh", {}), force),),
        "STARTING": (
            (("STATUS", job, {"status": "STARTING"}),),
            (force, ("STATUS", job, {"status": "STARTING"})),
        ),
        "RUNNING": ((force,), (("STATUS", job, {"status": "RUNNING"}),)),
        **{s: ((("STATUS", job, {"status": s}),),) for s in ("SUCCESS", "FAILURE", "TERMINATED")},
    }


def _feed_all(oracle: Oracle, steps: tuple[_Step, ...] | list[_Step]) -> None:
    """Feed the steps a minute apart. A step the oracle refuses is skipped:
    the caller reads what was reached, it does not assume it."""
    at = T0
    for kind, job, extra in steps:
        at += timedelta(minutes=1)
        try:
            oracle.feed(Event(at=at, kind=kind, payload={"job": job, **extra}))  # type: ignore[arg-type]
        except OracleError:
            pass


def _row_state(oracle: Oracle, job: str) -> tuple[object, ...]:
    """A job's status, its four flags and its holding state, and for
    a box the members taken off ice in its run (SEM-20), which a plain
    start of such a member meets."""
    row = oracle.store.job[job]
    holding = "none" if not row.reservations else "reserved" if row.status in LIVE else "held"
    return (
        job,
        row.status,
        row.on_ice,
        row.on_hold,
        row.on_noexec,
        row.armed,
        holding,
        tuple(sorted(row.iced_out_members)),
    )


def _job_builds() -> Iterator[tuple[list[_Step], tuple[str, ...], SemanticSwitches]]:
    """The input sequences for the targets, and the jobs each one's events
    address.

    Three jobs with a schedule and a condition that stays false, each
    holding one unit of another kind: `fj` a renewable FREE=N (a run's
    end keeps it held), `fa` a renewable FREE=A (freed), `fd` a depletable
    (spent). For each: held units (only `fj` can hold them), the arm (a
    tick blocked by the condition), ON_NOEXEC, then each status, then ice
    or hold. The box `fb` has an auto_hold member `fbm` with the same
    schedule and condition: the member's flag, arm (only while the box
    runs) or status, then the box's own flag, then each box status. The
    member carries box_terminator, so its FAILURE or TERMINATED in a
    running box ends the box (SEM-14). Last, the member iced, the box
    running, the member taken off ice: it sits the run out (SEM-20). A
    box started with its only member iced completes at its start under the
    default `box-start-all-members-out=complete` (SEM-11), so that build
    also runs under `wait`, where the forced box stays RUNNING and the
    OFF_ICE marks the member. Every other build runs under the defaults."""
    for job in ("fj", "fa", "fd"):
        recipes = _status_recipes(job)
        for held in (False, True) if job == "fj" else (False,):
            for armed in (False, True):
                for noexec in (False, True):
                    for status in _STATUSES:
                        for recipe in recipes[status]:
                            for flag in (None, "ON_ICE", "ON_HOLD"):
                                steps: list[_Step] = []
                                if held:
                                    steps += [
                                        ("FORCE_STARTJOB", job, {}),
                                        ("STATUS", job, {"status": "FAILURE"}),
                                    ]
                                if armed:
                                    steps.append(("STARTJOB", job, {}))
                                if noexec:
                                    steps.append(("ON_NOEXEC", job, {}))
                                steps += recipe
                                if flag is not None:
                                    steps.append((flag, job, {}))
                                yield steps, (job,), DEFAULTS
    fb = _status_recipes("fb")
    members: tuple[tuple[_Step, ...], ...] = (
        (),
        (("ON_NOEXEC", "fbm", {}),),
        (("ON_ICE", "fbm", {}),),
        (("ON_HOLD", "fbm", {}),),
        (("STATUS", "fb", {"status": "RUNNING"}), ("STARTJOB", "fbm", {})),
        *((("STATUS", "fbm", {"status": status}),) for status in _STATUSES[2:]),
    )
    for member in members:
        for flag in (None, "ON_ICE", "ON_HOLD", "ON_NOEXEC"):
            for status in _STATUSES:
                for recipe in fb[status]:
                    steps = list(member)
                    if flag is not None:
                        steps.append((flag, "fb", {}))
                    steps += recipe
                    yield steps, ("fb", "fbm"), DEFAULTS
    for switches in (DEFAULTS, _WAIT):
        for recipe in fb["RUNNING"]:
            steps = [("ON_ICE", "fbm", {}), *recipe, ("OFF_ICE", "fbm", {})]
            yield steps, ("fb", "fbm"), switches


def test_every_job_event_in_every_built_flag_and_holding_state_takes_a_declared_row(
    recorded: Path,
) -> None:
    """The `job` chart's flags and holding, over three scheduled resource
    holders and a box with an auto_hold member (`_job_builds`). Each distinct
    state the builder reaches (status, the four flags, the holding state; for
    a box, the member's too) gets every operator verb and an injected status
    of each kind, on the job and, for a box, on the member. The chart's
    status rows meet every move these events make.

    Left out: TIMER firings (term_run_time, must_start alarms) and the
    run_window deferrals, whose flag and holding moves are the start's and
    the end's, met here through STARTJOB and the injected statuses; the
    `queued-recheck` switch's path out of the queue (job.438 and
    job.439; the default re-checks nothing); a queued box or member, which DL-293's
    test builds; a nested box; the period opening, which has its own test
    below; and events addressed to another job that move the target: the
    condition predecessor's status or a SET_GLOBAL waking an armed target
    through the unscheduled start, a holder's end waking a queued one. A
    review probe sent 7254 such events over the 403 states the builder
    reached then and found no gap. The holding
    state `reserved` is only ever a live run's. The builder runs under the
    default switches, `off-ice-in-running-box=next-run` and
    `box-terminator-on-terminated=true`. Their other values add no state
    or move: `same-run` sets no mark, and `false` takes job.130 on
    FAILURE only, a move the default takes too. One build also runs under
    `box-start-all-members-out=wait` (see `_job_builds`); `complete` reaches
    no state of its own, since a box that completes at its start reads as
    one given SUCCESS. `idle-box-iced-member` adds no state or move: it
    decides whether an idle box re-derives through an iced member. Under
    `ignore`, the default these builds run, the moves that follow,
    job.446 to job.449, are taken through a member that is not iced, or
    the end of an iced member whose live status had blocked the re-derive.
    Under `vote` an iced member's own end can take them too, by design."""
    catalog = lower_source(_JOB_JIL)
    reached: set[tuple[object, ...]] = set()
    applied = 0
    for steps, targets, switches in _job_builds():
        base = Oracle(catalog, semantics=switches)
        _feed_all(base, steps)
        state = tuple(_row_state(base, job) for job in targets)
        if state in reached:
            continue
        reached.add(state)
        for job in targets:
            for kind, extra in _JOB_EVENTS:
                twin = base.fork()
                applied += 1
                try:
                    twin.feed(Event(at=T0 + DAY, kind=kind, payload={"job": job, **extra}))  # type: ignore[arg-type]
                except OracleError:
                    pass  # the oracle's own refusal
    assert len(reached) == 405 and applied >= 10_000
    # not vacuous: two states hold a member taken off ice in its running box
    # (the box started by a force, so the member is auto-held, or by a status)
    assert sum(1 for state in reached if state[0][-1]) == 2
    # not vacuous: the events took every flag, arm and holding row but the
    # queued-recheck leaves (job.438, job.439) and the opening release
    # (job.308, see above) and the engine's interruption hold (job.239, a FAILURE no operator
    # can send: test_job_chart's cells and the oracle goldens take it, DL-424); the arm's rows
    # are job.429 to job.439 (DL-365)
    taken = _hits(recorded)
    arm = {f"job.{n}" for n in range(429, 440)}
    flags_and_holding = {
        row.id for row in JOB.rows if (row.id[4] in "23" or row.id in arm) and not row.mark
    }
    assert flags_and_holding - taken == {"job.438", "job.439", "job.308", "job.239"}


@pytest.mark.parametrize("holds", [True, False])
def test_a_period_opening_over_a_removed_job_takes_a_declared_holding_row(
    recorded: Path, holds: bool
) -> None:
    """job.308's event: the period's first input gives back the
    units a removed job holds (DL-256). The carried row is built as the
    opening builds it; a removed job that holds nothing is owed nothing and
    takes no move. A live removed job is refused at the boundary
    (period-model ss10.1), so it is not built."""
    units = (CapacityReservation(bucket="r:XR", units=1, release_policy="never"),)
    carried = CarriedRows(
        jobs={"gone": JobRuntime(status="FAILURE", reservations=units if holds else ())},
        period_id=2,
        now=T0,
    )
    oracle = Oracle(lower_source(_JOB_JIL), carried=carried)
    oracle.feed(Event(at=T0 + DAY, kind="SET_GLOBAL", payload={"name": "G", "value": "x"}))
    assert oracle.store.job["gone"].reservations == ()
    assert ("job.308" in _hits(recorded)) is holds


# ---------------------------------------------------------- runtime_assembly

#: the verb sequences that build each assembly phase on a fresh state, as
#: the Oracle's constructor, the period opener and the inputs do
_ASSEMBLY_BUILDS: tuple[tuple[str, ...], ...] = (
    (),
    ("install",),
    ("begin_input",),
    ("begin_input", "commit_input"),
    ("install", "begin_input", "commit_input"),
    ("finish_genesis",),
    ("begin_input", "commit_input", "finish_genesis"),
    ("seed_period",),
    ("install", "seed_period"),
    ("finish_genesis", "seed_period"),
    ("finish_genesis", "begin_input"),
    ("seed_period", "begin_input"),
    ("finish_genesis", "begin_input", "commit_input"),
    ("finish_genesis", "begin_input", "commit_input", "begin_input"),
)
#: what each verb does, with a period id of 1 or 0 for `seed_period`
_ASSEMBLY_VERBS: dict[str, Callable[[RuntimeState], object]] = {
    "install": lambda store: store.install(CarriedRows()),
    "begin_input": RuntimeState.begin_input,
    "commit_input": RuntimeState.commit_input,
    "finish_genesis": RuntimeState.finish_genesis,
    "seed_period": lambda store: store.seed_period(1),
    "seed_period(0)": lambda store: store.seed_period(0),
}
_ASSEMBLY_PHASES: set[str] = {
    "fresh",
    "installed",
    "genesis_input",
    "genesis",
    "constructed",
    "seeded",
    "input",
    "live",
}
#: the cells the constructor's order and DL-132 allow: a verb that moves
#: the phase. Every other cell of the eight phases by five verbs is a
#: refusal row of the chart.
_ASSEMBLY_ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        ("fresh", "install"),
        *((phase, "begin_input") for phase in ("fresh", "installed", "genesis")),
        ("genesis_input", "commit_input"),
        *((phase, "finish_genesis") for phase in ("fresh", "installed", "genesis")),
        *((phase, "seed_period") for phase in ("fresh", "installed", "constructed")),
        *((phase, "begin_input") for phase in ("constructed", "seeded", "live")),
        ("input", "commit_input"),
    }
)


def test_every_assembly_verb_in_every_phase_meets_a_row_or_a_refusal(
    recorded_privately: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`runtime_assembly`: all eight phases, built on a `RuntimeState` by
    its own verbs (`_ASSEMBLY_BUILDS`), and each of its five verbs (and a
    `seed_period(0)`) in each. The chart's outcome decides: a verb fires a
    row, or a refusal row raises before the phase moves; a `seed_period(0)`
    in a phase that allows it fires and then fails its own check, and the
    phase stays. Left out: nothing; every phase is built. Its hits stay
    private: most of these verb orders are no production path."""
    outcomes: list[Outcome] = []
    dispatch = Executor.dispatch
    resolve = Executor.resolve

    def spy(self: Executor, key: object, event: Any) -> list[Outcome]:
        result = dispatch(self, key, event)
        outcomes.extend(result)
        return result

    def spy_read(self: Executor, key: object, event: Any) -> Outcome:
        result = resolve(self, key, event)
        outcomes.append(result)
        return result

    monkeypatch.setattr(Executor, "dispatch", spy)
    monkeypatch.setattr(Executor, "resolve", spy_read)
    phases: set[str] = set()
    refused: set[tuple[str, str]] = set()
    allowed: set[tuple[str, str]] = set()
    for build in _ASSEMBLY_BUILDS:
        for verb, move in _ASSEMBLY_VERBS.items():
            store = RuntimeState()
            for name in build:
                _ASSEMBLY_VERBS[name](store)
            phase = store._phase
            phases.add(phase)
            outcomes.clear()
            try:
                move(store)
                failed = False
            except ValueError:  # an OracleError is a ValueError
                failed = True
            event = verb.removesuffix("(0)")
            (outcome, *_) = outcomes
            assert outcome.event.name == event
            if outcome.refusals:
                refused.add((phase, event))
                assert failed
                assert store._phase == phase  # a refusal: nothing moved
            else:
                allowed.add((phase, event))
                assert failed is (verb == "seed_period(0)")  # the verb's own check
                assert (store._phase == phase) is failed
    assert phases == _ASSEMBLY_PHASES
    assert allowed == _ASSEMBLY_ALLOWED
    assert (
        refused
        == {
            (phase, event)
            for phase in _ASSEMBLY_PHASES
            for event in ("install", "begin_input", "commit_input", "finish_genesis", "seed_period")
        }
        - _ASSEMBLY_ALLOWED
    )


# ------------------------------------------------------------------ engine

_HOST_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"


def _run_spied(outcomes: list[Outcome]) -> Executor:
    """An engine's executor that keeps every outcome: a start and a post both
    end in `run`."""
    machines = Executor()
    run = machines.run

    def spy(after: Any = None) -> list[Outcome]:
        taken = run(after)
        outcomes.extend(taken)
        return taken

    machines.run = spy  # type: ignore[method-assign]
    return machines


def _spied(outcomes: list[Outcome]) -> Executor:
    """A stand-in for the engine's executor that keeps every outcome."""
    machines = Executor()
    dispatch = machines.dispatch

    def spy(key: Any, event: ChartEvent) -> list[Outcome]:
        taken = dispatch(key, event)
        outcomes.extend(taken)
        return taken

    machines.dispatch = spy  # type: ignore[method-assign]
    return machines


def _feeds(path: Path) -> Iterator[tuple[Journal, Any, list[Outcome]]]:
    """Each feed state the journal and the handler reach, by their own
    calls: a new feed in backfill, live after the backfill, each with room
    or with a backlog its budget cannot grow, removed by an overflow from
    either, and closed from either. Each comes with the executor's outcomes
    so far."""
    for live in (False, True):
        for tight in (False, True):
            outcomes: list[Outcome] = []
            journal = Journal(MemoryStore())
            feed = journal.subscribe(_spied(outcomes), budget=1 if tight else 1 << 30)
            journal.preflight([], datetime(2026, 7, 1, 8, 0))  # an empty backlog takes one record
            if live:
                feed.go_live()
            yield journal, feed, outcomes
    for live in (False, True):
        for end in ("overflow", "unsubscribe"):
            outcomes = []
            journal = Journal(MemoryStore())
            feed = journal.subscribe(_spied(outcomes), budget=1)
            journal.preflight([], datetime(2026, 7, 1, 8, 0))
            if live:
                feed.go_live()
            if end == "overflow":
                journal.preflight([], datetime(2026, 7, 1, 8, 0))
            else:
                journal.unsubscribe(feed)
            yield journal, feed, outcomes


def _feed_row(state: str, event: str, tight: bool) -> tuple[str, ...]:
    """The rows one event takes, from control-protocol ss5 and the block doc:
    a record that fits is queued, one that does not removes the feed, the
    backfill sent moves it live, and its end closes it. A removed or closed
    feed has left the fan-out, so nothing posts to it."""
    if state in ("removed", "closed"):
        return ()
    if event == "append":
        return ("subscription.03",) if tight else ("subscription.05",)
    return ("subscription.04",) if event == "unsubscribe" else ("subscription.02",)


def test_every_feed_event_in_every_subscription_state_takes_a_declared_transition(
    recorded: Path, tmp_path: Path
) -> None:
    """`subscription`: every state a feed reaches through the journal and the
    handler (`_feeds`), and in each the events the journal applies to any
    feed: an append, and the stream's end (`unsubscribe`). In `backfill`
    also the backfill sent (`go_live`). Each post is one microstep of the
    executor that fires the declared row; a removed or closed feed gets no
    post at all, so no final state's implicit ignore is met.

    Left out: `go_live` in `live`, `removed` and `closed`. The handler
    (`ControlServer._subscribe`) calls it once, after the ack and the
    backfill, with no await between the last backfill send and the call;
    an overflow cancels the handler, and the cancel lands at its next
    await, before `go_live`; and only the handler's `finally` ends the
    stream. So `go_live` meets only `backfill`."""
    states: set[str] = set()
    for journal, feed, _ in _feeds(tmp_path):
        journal.close()
        states.add(feed.state)
    assert states == {"backfill", "live", "removed", "closed"}
    taken: set[str] = set()
    for event in ("append", "unsubscribe", "go_live"):
        (tmp_path / event).mkdir()
        for journal, feed, outcomes in _feeds(tmp_path / event):
            state, tight, before = feed.state, feed.budget == 1, len(outcomes)
            try:
                if event == "append":
                    journal.preflight([], datetime(2026, 7, 1, 8, 0))
                elif event == "unsubscribe":
                    journal.unsubscribe(feed)
                elif state == "backfill":
                    feed.go_live()
                else:
                    continue
            finally:
                journal.close()
            cell = outcomes[before:]
            assert [o.kind for o in cell] == (["fired"] if cell else []), (state, event)
            rows = tuple(row for outcome in cell for row in outcome.rows)
            assert rows == _feed_row(state, event, tight), (state, event, tight)
            taken |= set(rows)
    assert taken == {row.id for row in SUBSCRIPTION.rows if row.kind != "ignored"}


# -------------------------------------------------------------- admission

#: the request id every admission case addresses. A client id: the
#: `engine:` prefix is the engine's own, and the envelope refuses it
_R = "r-1"
_G = "global:G"


def _set_global(value: str) -> Event:
    return Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": value})


#: (event, expect) of each operator command an admission case sends
type _Command = tuple[Event, dict[str, int]]
_APPLIES: _Command = (_set_global("1"), {_G: 0})
_REJECTED: _Command = (_set_global("1"), {_G: 5})


def _send(engine: Engine, command: _Command) -> asyncio.Future[ApplyResult]:
    what, expect = command
    envelope = Envelope(request_id=_R, expect=expect)
    return engine.submit(what.model_copy(), envelope)  # type: ignore[return-value]


def _admission_events() -> dict[str, _Command | None]:
    """Every input that can address `_R`: the command that built the state
    (or, for an unseen id, one that applies), another command, a failed
    precondition, a command that faults when applied, and (None) an input
    the engine makes. That one takes a name
    of its own, so it cannot address `_R`: it must leave the id where it was."""
    return {
        "the same command": None,
        "another command": (_set_global("2"), {_G: 0}),
        "a failed precondition": (_set_global("3"), {_G: 7}),
        "a faulting command": (Event(at=T0, kind="STARTJOB", payload={}), {"job:j": 0}),
        "an engine-made input": None,
    }


def _r_state(engine: Engine) -> str:
    """`_R`'s state: the stored decision, the open instance's leaf, or
    `unseen` for an id the index never saw."""
    return engine.decisions.state(_R) or "unseen"


async def _admission_case(state: str, event: str) -> tuple[str, str, tuple[str, ...]]:
    """Build `_R` in `state` at index 1, send `event`, and answer how it
    ended, the id's state after it, and what the engine's executor reported
    for `_R`'s instance meanwhile: `started`, or the row a post fired."""
    outcomes: list[Outcome] = []
    engine = Engine(
        lower_source(_HOST_JIL),
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter()},
        machines=_run_spied(outcomes),
    )

    def posts() -> tuple[str, ...]:
        key = engine.decisions.key(_R)
        return tuple(
            o.kind if o.kind == "started" else o.rows[0] for o in outcomes[before:] if o.key == key
        )

    try:
        built = {"applied": _APPLIES, "rejected": _REJECTED}.get(state, _APPLIES)
        if state in ("applied", "rejected"):
            _send(engine, built)
        else:  # index 1 goes to an unrelated input the engine makes
            engine.inject(
                Event(at=T0, kind="SET_GLOBAL", payload={"name": "X", "value": "0"}),
                source="scheduler",
            )
        await engine.run_until_quiescent(T0)
        if state == "admitted":
            # an attempt line with no decision, as `read_decisions` notes one
            what, expect = built
            engine.decisions.note(
                Attempt(
                    index=2,
                    at=T0,
                    request_id=_R,
                    fingerprint=fingerprint(
                        baseline_id=engine.baseline_id,
                        kind=what.kind,
                        payload=dict(what.payload),
                        source="control",
                        expect=expect,
                    ),
                )
            )
        assert _r_state(engine) == state
        before = len(outcomes)
        command = _admission_events()[event]
        if event == "an engine-made input":
            engine.inject(_set_global("9"), source="scheduler")
            future = None
        else:
            future = _send(engine, built if command is None else command)
        try:
            await engine.run_until_quiescent(T0)
        except EngineError as exc:  # the loop's own stop: a second writer
            return f"stopped: {exc}", _r_state(engine), posts()
        if future is None:
            return "made", _r_state(engine), posts()
        try:
            return (await future).decision, _r_state(engine), posts()
        except EngineError as exc:
            return f"refused {exc.code}", _r_state(engine), posts()
    finally:
        await engine.shutdown()


def _wire_refuses_an_engine_name() -> bool:
    """The door: `parse_envelope` refuses a client request id that is one
    of the engine's own names, as an invalid argument."""
    request = {
        "v": PROTOCOL_VERSION,
        "baseline_id": "b",
        "request_id": "engine:2",
        "expect": {_G: 0},
    }
    try:
        parse_envelope(request, addressed=_G, baseline_id="b")
    except EnvelopeError as exc:
        return exc.code == "invalid_argument" and "engine:" in str(exc)
    return False


#: what an unseen id's admitted command is decided, by concurrency-model ss4
#: steps 5-7: `expect` holds for the command that built the state and another
#: command, and fails for the failed precondition
_UNSEEN_DECISIONS = {
    "the same command": "applied",
    "another command": "applied",
    "a failed precondition": "rejected",
}
#: what refuses an unseen id's command before admission (ss4 steps 2-3)
_UNSEEN_REFUSALS = {"a faulting command": "apply_faulted"}


def _admission_cell(state: str, event: str) -> tuple[str, str, tuple[str, ...]]:
    """One cell, written from `(state, event)` and concurrency-model ss4,
    without the rows and without the observed answer: the answer, `_R`'s
    state after it, and what `_R`'s instance reports. An id is in flight from
    its attempt line to its decision. An engine-made input has a name of its
    own: it is made and posts nothing for `_R`. An unseen id refused before
    admission starts nothing; one admitted starts and takes its decision. A
    decided id is answered by the index from the stored decision, retry or
    reuse, with no post (DL-217, CM-05). An id in flight refuses every other
    command (`request_id_reused`) and stops the engine on the same command:
    a second writer. A `stopped`
    answer is matched as a prefix."""
    if event == "an engine-made input":
        return "made", state, ()
    if state == "unseen":
        if event in _UNSEEN_REFUSALS:
            return f"refused {_UNSEEN_REFUSALS[event]}", "unseen", ()
        decision = _UNSEEN_DECISIONS[event]
        row = "admission.02" if decision == "applied" else "admission.03"
        return decision, decision, ("started", row)
    if state == "admitted":
        if event == "the same command":
            return "stopped", "admitted", ("admission.10",)
        return "refused request_id_reused", "admitted", ("admission.09",)
    if event == "the same command":
        return state, state, ()
    return "refused request_id_reused", state, ()


def test_every_input_in_every_request_id_state_takes_a_declared_transition(recorded: Path) -> None:
    """`admission`: one request id in each of its four states, built by a
    live engine (`unseen`, `applied`, `rejected`), and `admitted` as
    `read_decisions` leaves an attempt line with no decision (the engine
    itself never meets it at dedup: steps 5-7 do not yield to another
    input). Every input that can address the id (`_admission_events`) in
    each. The engine's executor reports what `_R`'s instance did: a start
    and a decision row, a refusal row, the second-writer row, or nothing,
    where the index answered a decided id or a refusal came before
    admission. A gap in the chart would raise a statechart error.

    Left out: replay's moves (`read_decisions` notes every attempt and
    records every decision, `replay_inputs` records a recovered one),
    whose one collision the reserved `engine:` prefix closes
    (`test_a_client_request_id_in_the_engines_prefix_is_refused`)."""
    outcomes: dict[tuple[str, str], tuple[str, str, tuple[str, ...]]] = {}
    missing: set[tuple[str, str]] = set()
    for state in ("unseen", "applied", "rejected", "admitted"):
        for event in _admission_events():
            try:
                outcomes[(state, event)] = asyncio.run(_admission_case(state, event))
            except StatechartError:
                missing.add((state, event))
    assert missing == set()
    assert _wire_refuses_an_engine_name()
    for state in ("unseen", "applied", "rejected", "admitted"):
        # an engine-made input has a name of its own and leaves the id alone
        assert outcomes[(state, "an engine-made input")] == ("made", state, ())
    assert outcomes[("unseen", "the same command")][:2] == ("applied", "applied")
    assert outcomes[("applied", "the same command")] == ("applied", "applied", ())
    assert outcomes[("rejected", "a failed precondition")][0] == "refused request_id_reused"
    assert outcomes[("unseen", "a faulting command")] == ("refused apply_faulted", "unseen", ())
    assert outcomes[("admitted", "the same command")][0].startswith("stopped:")
    for cell, (answer, after, posted) in outcomes.items():
        expected, expected_after, expected_posts = _admission_cell(*cell)
        if expected == "stopped":
            answer = "stopped" if answer.startswith("stopped:") else answer
        assert (answer, after, posted) == (expected, expected_after, expected_posts), cell
    taken = {row for _, _, posted in outcomes.values() for row in posted} - {"started"}
    assert taken == {row.id for row in ADMISSION.rows if row.kind != "ignored"}


# ------------------------------------- the engine's reserved request-id prefix


@pytest.mark.parametrize(
    ("request_id", "refused"),
    [
        ("engine:1", True),
        ("engine:", True),
        ("engine:2026-07-01T08:00:00", True),  # the other name the engine makes
        ("engine", False),  # no colon: not the prefix
        ("Engine:1", False),  # ids are case-sensitive, as the engine's names are
        ("xengine:1", False),  # the prefix must begin the id
        (" engine:1", False),  # as must it begin the id after whitespace
        ("r-engine:1", False),
    ],
)
def test_a_client_request_id_in_the_engines_prefix_is_refused(
    request_id: str, refused: bool
) -> None:
    """Triggering: an id that begins with `engine:`. Non-triggering: an id
    that only contains it, differs in case, or lacks the colon. Each is a
    legal client id, and none can equal a name the engine makes."""
    request = {
        "v": PROTOCOL_VERSION,
        "baseline_id": "b",
        "request_id": request_id,
        "expect": {_G: 0},
    }
    if refused:
        with pytest.raises(EnvelopeError, match="reserves") as caught:
            parse_envelope(request, addressed=_G, baseline_id="b")
        assert caught.value.code == "invalid_argument"
    else:
        assert parse_envelope(request, addressed=_G, baseline_id="b").request_id == request_id


def test_no_client_request_collides_with_an_engine_made_input_live_or_on_replay(
    recorded: Path, short_root: Path
) -> None:
    """The collision the prefix closes, end to end. An operator tries the
    name the engine would give index 2 on `sendevent`: it is refused on the
    wire and nothing reaches the log. A legal request
    takes index 1, the engine then makes input 2 and names it
    `engine:2`, and the client's decision stands: its retry is answered
    from it. Replay of the log notes every attempt."""
    from test_runner_control import _control_call, _read_revision, _serve, _teardown

    async def scenario() -> tuple[list[dict[str, Any]], bytes, list[dict[str, Any]]]:
        engine, server, loop_task = await _serve(short_root / "run", _HOST_JIL)
        try:
            assert engine.journal is not None
            baseline, revision = await _read_revision(server.path, _G)
            legal = command(
                "SET_GLOBAL",
                {"name": "G", "value": "1"},
                key=_G,
                revision=revision,
                baseline_id=baseline,
                request_id="r-1",
            )
            before = engine.journal.path.read_bytes()
            refusals = [await _control_call(server.path, {**legal, "request_id": "engine:2"})]
            assert engine.journal.path.read_bytes() == before  # nothing was admitted
            first = await _control_call(server.path, legal)
            assert first["ok"] is True and first["decision"] == "applied"
            engine.inject(
                Event(
                    at=engine.clock.now(), kind="SET_GLOBAL", payload={"name": "E", "value": "1"}
                ),
                source="scheduler",
            )
            for _ in range(200):
                if engine.decisions.state("engine:2") == "applied":
                    break
                await asyncio.sleep(0.02)
            assert engine.decisions.state(f"{ENGINE_REQUEST_ID_PREFIX}2") == "applied"
            retry = await _control_call(server.path, legal)
            assert retry["ok"] is True and retry["decision"] == "applied"
            assert "refused" not in retry or retry["refused"] is False
            return refusals, engine.journal.path.read_bytes(), read_journal(engine.journal.path)
        finally:
            await _teardown(engine, server, loop_task)

    refusals, _raw, records = asyncio.run(scenario())
    for answer in refusals:
        assert answer["ok"] is False and answer["refused"] is True
        assert answer["code"] == "invalid_argument" and "engine:" in answer["error"]
    # replay: every attempt noted under its own id, each decided once
    seqs = {r["request_id"]: r["seq"] for r in records if r.get("rec") == "input"}
    assert seqs == {"r-1": 1, "engine:2": 2}
    index = read_decisions(records)
    assert index.state("r-1") == "applied"
    assert index.state("engine:2") == "applied"
