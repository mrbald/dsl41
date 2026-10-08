"""Every event in every built state, for the engine tier's machines and the
job's flags, holding and assembly (DL-289, DL-293, DL-295).

Transition coverage proves that each declared transition is taken
somewhere. It does not prove that a table is complete: a move in a state
the suite never builds would go unseen until production, where the
default `refuse` policy refuses a legitimate control input. So each test
here builds the states its machine's code reaches, applies every event
that code accepts in each of them, under the strict variable the test
sets itself, and records into its own directory. An event either takes a
declared transition or is refused by the code's own rule. A
`TransitionError` outside the refusals a test lists means a row is
missing. Each test then checks that no violation was recorded: no
`TRANSITION_VIOLATION` trace line, no stderr line under
`VIOLATION_LOG_PREFIX`, and no violations file entry beyond the listed
refusals. Each docstring says what its builder leaves out.
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

from dsl41 import runner
from dsl41.ast_jil import parse, render_preserve
from dsl41.boundary import SealRequest, default_anchor_dir, stage_next_period
from dsl41.ir import lower_catalog, lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import (
    JOB_FLAGS,
    JOB_HOLDING,
    LIVE,
    VIOLATION_MARKER,
    CapacityReservation,
    CarriedRows,
    Event,
    JobRuntime,
    OracleError,
    RuntimeState,
)
from dsl41.period import RuntimeProfile, SourceFile, stage_manifest, write_bundle
from dsl41.runner import SEAL_BOUNDARY, Engine, _PendingSeal
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    ADMISSION,
    ENGINE_REQUEST_ID_PREFIX,
    PROTOCOL_VERSION,
    ApplyResult,
    Attempt,
    Envelope,
    EnvelopeError,
    TransitionStop,
    apply_attempt,
    fingerprint,
    parse_envelope,
)
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_effects import EFFECT, Effect, EffectOutcome, Outbox, effect_id_for
from dsl41.runner_hosts import HOST, LOCAL_EXECUTOR_ID, HostCommand, seed_local_executor
from dsl41.runner_control import command
from dsl41.runner_journal import SUBSCRIPTION, Journal, read_decisions, read_journal
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_startup import start_run
from dsl41.seal import StagedNextPeriod
from dsl41.state_machine import (
    HITS_ENV,
    STRICT_ENV,
    VIOLATION_LOG_PREFIX,
    TransitionError,
)

T0 = datetime(2026, 7, 1, 8, 0)
DAY = timedelta(days=1)


def _recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, forward: bool) -> Iterator[Path]:
    """The strict variable, set here, and a private record directory, so a
    refusal the table makes by design stays out of the session's gate.

    With `forward`, the hits are handed to the session's directory
    afterwards, when there is one, so the gate's per-source report sees the
    states built here. Forward only from a test whose cells take declared
    transitions the production code can take: a hit from a verb order no
    production path makes would hide, in that report, a table source wider
    than production uses. Violations are never forwarded."""
    session = os.environ.get(HITS_ENV)
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    monkeypatch.setenv(STRICT_ENV, "1")
    yield directory
    hits = [
        line for path in directory.glob("hits-*.jsonl") for line in path.read_text().splitlines()
    ]
    if forward and session and hits:
        with open(Path(session) / f"hits-{os.getpid()}-exhaustive.jsonl", "a") as out:
            out.write("\n".join(hits) + "\n")


@pytest.fixture
def strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """`_recording`, with the hits forwarded."""
    yield from _recording(tmp_path, monkeypatch, forward=True)


@pytest.fixture
def strict_unforwarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """`_recording`, with the hits kept private: the cells build states by
    verb orders no production path makes."""
    yield from _recording(tmp_path, monkeypatch, forward=False)


def _violations(directory: Path) -> set[tuple[str, str, str, str]]:
    """Every violation recorded into `directory`: (machine, id, old, new)."""
    return {
        (record["machine"], record["id"], record["old"], record["new"])
        for path in directory.glob("violations-*.jsonl")
        for record in map(json.loads, path.read_text().splitlines())
    }


def _hits(directory: Path) -> set[str]:
    """The transition ids taken and recorded into `directory`."""
    return {
        record["id"]
        for path in directory.glob("hits-*.jsonl")
        for record in map(json.loads, path.read_text().splitlines())
    }


def _no_violation_line(err: str) -> None:
    assert [line for line in err.splitlines() if line.startswith(VIOLATION_LOG_PREFIX)] == []


# --------------------------------------------- job_flags and job_holding

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
    + _SCHEDULED
)
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
    """A job's status, its four flags and its `job_holding` state."""
    row = oracle.store.job[job]
    holding = "none" if not row.reservations else "reserved" if row.status in LIVE else "held"
    return (job, row.status, row.on_ice, row.on_hold, row.on_noexec, row.armed, holding)


def _job_builds() -> Iterator[tuple[list[_Step], tuple[str, ...]]]:
    """The input sequences for the targets, and the jobs each one's events
    address.

    Three jobs with a schedule and a condition that stays false, each
    holding one unit of another kind: `fj` a renewable FREE=N (a run's
    end keeps it held), `fa` a renewable FREE=A (freed), `fd` a depletable
    (spent). For each: held units (only `fj` can hold them), the arm (a
    tick blocked by the condition), ON_NOEXEC, then each status, then ice
    or hold. The box `fb` has an auto_hold member `fbm` with the same
    schedule and condition: the member's flag, arm (only while the box
    runs) or status, then the box's own flag, then each box status."""
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
                                yield steps, (job,)
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
                    yield steps, ("fb", "fbm")


def test_every_job_event_in_every_built_flag_and_holding_state_takes_a_declared_transition(
    strict: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`job_flags` and `job_holding`, over three scheduled resource holders
    and a box with an auto_hold member (`_job_builds`). Each distinct state the
    builder reaches (status, the four flags, the holding state; for a box,
    the member's too) gets every operator verb and an injected status of
    each kind, on the job and, for a box, on the member. The strict run
    also checks `job_status` on every move these events make.

    Left out: TIMER firings (term_run_time, must_start alarms) and the
    run_window deferrals, whose flag and holding moves are the start's and
    the end's, met here through STARTJOB and the injected statuses; the
    `queued-recheck` switch's path out of the queue (job_flags.16; the
    default re-checks nothing); a queued box or member, which DL-293's
    test builds; a nested box; the period opening, which has its own test
    below; and events addressed to another job that move the target: the
    condition predecessor's status or a SET_GLOBAL waking an armed target
    through the unscheduled start, a holder's end waking a queued one. A
    review probe sent 7254 such events over these 403 states under the
    strict variable and found no gap. The holding state `reserved` is only
    ever a live run's."""
    catalog = lower_source(_JOB_JIL)
    reached: set[tuple[object, ...]] = set()
    missing: list[str] = []
    applied = 0
    for steps, targets in _job_builds():
        base = Oracle(catalog)
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
                except TransitionError as exc:
                    missing.append(f"{kind} {extra} on {job} in {state}: {exc}")
                if any(e.transition == VIOLATION_MARKER for e in twin.trace()):
                    missing.append(f"{kind} {extra} on {job} in {state}: a violation line")
    assert missing == []
    assert _violations(strict) == set()
    _no_violation_line(capsys.readouterr().err)
    assert len(reached) == 403 and applied >= 10_000
    # not vacuous: the events took every flag and holding move but the
    # queued-recheck disarm and the opening release (see above)
    taken = _hits(strict)
    assert {t.id for t in JOB_FLAGS.transitions} - taken == {"job_flags.16"}
    assert {t.id for t in JOB_HOLDING.transitions} - taken == {"job_holding.08"}


@pytest.mark.parametrize("holds", [True, False])
def test_a_period_opening_over_a_removed_job_takes_a_declared_holding_move(
    strict: Path, holds: bool
) -> None:
    """`job_holding.08`'s event: the period's first input gives back the
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
    assert not any(e.transition == VIOLATION_MARKER for e in oracle.trace())
    assert _violations(strict) == set()
    assert ("job_holding.08" in _hits(strict)) is holds


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
#: the moves the table leaves out by design, which `_assemble` refuses with
#: the table's own check (DL-293): a commit with no input open, and the
#: end of construction inside the genesis input. No production path makes
#: either. Every other refusal is a guard that raises before the table.
_ASSEMBLY_REFUSED: frozenset[tuple[str, str]] = frozenset(
    {
        *(
            (phase, "commit_input")
            for phase in ("fresh", "installed", "genesis", "constructed", "seeded", "live")
        ),
        ("genesis_input", "finish_genesis"),
    }
)


def test_every_assembly_verb_in_every_phase_takes_a_declared_transition(
    strict_unforwarded: Path,
) -> None:
    """`runtime_assembly`: all eight phases, built on a `RuntimeState` by
    its own verbs (`_ASSEMBLY_BUILDS`), and each of its five verbs (and a
    `seed_period(0)`) in each. A verb takes a declared transition, or its
    guard refuses it (ValueError, OracleError) before the table is asked,
    or the table refuses one of the moves it leaves out by design
    (`_ASSEMBLY_REFUSED`). Left out: nothing; every phase is built. Its hits
    stay private: most of these verb orders are no production path."""
    phases: set[str] = set()
    refused: set[tuple[str, str]] = set()
    for build in _ASSEMBLY_BUILDS:
        for verb, move in _ASSEMBLY_VERBS.items():
            store = RuntimeState()
            for name in build:
                _ASSEMBLY_VERBS[name](store)
            phase = store._phase
            phases.add(phase)
            try:
                move(store)
            except (ValueError, OracleError):
                assert store._phase == phase  # a guard: nothing moved
            except TransitionError:
                refused.add((phase, verb))
                assert store._phase == phase
    assert phases == _ASSEMBLY_PHASES
    assert refused == _ASSEMBLY_REFUSED
    commits = {p for p, verb in _ASSEMBLY_REFUSED if verb == "commit_input"}
    assert _violations(strict_unforwarded) == {
        *(("runtime_assembly", "runtime_assembly.07", p, "live") for p in commits),
        ("runtime_assembly", "runtime_assembly.04", "genesis_input", "constructed"),
    }


# ------------------------------------------------------------------- host

_HOST_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
_ACTOR = "alice@ops"
#: (verb, force, seconds after T0) for each step that builds a routing state
type _HostStep = tuple[str, bool, int]
_HOST_BUILDS: tuple[tuple[_HostStep, ...], ...] = (
    (),
    (("drain", False, 0),),
    (("quarantine", False, 0),),
    (("drain", False, 0), ("quarantine", False, 0)),
    (("quarantine", False, 0), ("reinstate", False, 0)),
    (("drain", False, 0), ("quarantine", False, 0), ("reinstate", False, 0)),
    (("evict", True, 0),),
    (("drain", False, 0), ("evict", True, 0)),
    (("quarantine", False, 0), ("evict", True, 0)),
    (("quarantine", False, 0), ("evict", False, 3600)),
)
_HOST_VERBS = ("activate", "drain", "evict", "quarantine", "reinstate")


def _host_attempt(
    index: int, verb: str, *, force: bool, at: datetime, actor: str | None, host: str
) -> Attempt:
    return Attempt(
        index=index,
        at=at,
        request_id=f"r{index}",
        fingerprint="fp",
        host=HostCommand(verb=verb, host_id=host, force=force),  # type: ignore[arg-type]
        claimed_actor=actor,
    )


def _host_events(now: datetime) -> Iterator[tuple[str, Attempt, ApplyResult | None]]:
    """Every host command: each verb, with and without force, with and
    without an actor, at `now` and a day later, gated live; each
    verb and force again as a replayed durable `applied` (the apply half,
    which skips the guards); and each verb for a host the table does not
    hold."""
    index = 100
    for verb in _HOST_VERBS:
        for force in (False, True):
            for actor in (None, _ACTOR):
                for at in (now, now + DAY):
                    index += 1
                    yield (
                        f"{verb} force={force} actor={actor} at={at}",
                        _host_attempt(
                            index, verb, force=force, at=at, actor=actor, host=LOCAL_EXECUTOR_ID
                        ),
                        None,
                    )
            index += 1
            yield (
                f"replayed applied {verb} force={force}",
                _host_attempt(
                    index, verb, force=force, at=now, actor=_ACTOR, host=LOCAL_EXECUTOR_ID
                ),
                ApplyResult(index=index, request_id=f"r{index}", decision="applied"),
            )
        index += 1
        yield (
            f"{verb} on an unknown host",
            _host_attempt(index, verb, force=False, at=now, actor=_ACTOR, host="nowhere"),
            None,
        )


def test_every_host_command_in_every_built_routing_state_takes_a_declared_transition(
    strict: Path,
) -> None:
    """`host`: the routing states the verbs build through `apply_attempt`,
    the path the engine and replay share (`_HOST_BUILDS`): active and
    passive (also after a quarantine and reinstate), quarantined from
    either, evicted by force from each of the three, and evicted past the
    ss8 bound; each with and without a deadman. Every host command
    (`_host_events`) in each. A command takes a declared transition, or the
    gate rejects it (a decision that moves nothing), or the apply half
    refuses a durable decision the table has no transition for (its own
    EngineError, naming it; a replay check that the stored revisions match
    is an EngineError too, raised after the move).

    Left out: a carried row with no `last_contact` (only the
    `host_never_contacted` rejection reads it, and a rejection takes no
    transition), a stale `expect` (the precondition rejects before the
    host gate), and the relay's re-registration (host.12, spec-only)."""
    catalog = lower_source(_HOST_JIL)
    reached: set[tuple[object, ...]] = set()
    missing: list[str] = []
    for deadman in (None, 60.0):
        for build in _HOST_BUILDS:
            base = Oracle(catalog)
            seed_local_executor(base.store, LOCAL_EXECUTOR_ID, at=T0, deadman_s=deadman)
            for index, (verb, force, offset) in enumerate(build, start=1):
                step = _host_attempt(
                    index,
                    verb,
                    force=force,
                    at=T0 + timedelta(seconds=offset),
                    actor=_ACTOR,
                    host=LOCAL_EXECUTOR_ID,
                )
                apply_attempt(base, step, grace_s=10.0)
            row = base.store.hosts[LOCAL_EXECUTOR_ID]
            state = (
                row.state,
                row.deadman_s,
                row.forced_by,
                row.generation,
                row.state_before_quarantine,
            )
            if state in reached:
                continue
            reached.add(state)
            for name, attempt, decided in _host_events(base._now or T0):
                twin = base.fork()
                try:
                    applied = apply_attempt(twin, attempt, decided=decided, grace_s=10.0)
                except EngineError as exc:
                    assert "no transition for it" in str(exc) or "replay diverged" in str(exc)
                    continue
                except TransitionError as exc:
                    missing.append(f"{name} on {state}: {exc}")
                    continue
                assert applied.violations == []
                if any(e.transition == VIOLATION_MARKER for e in twin.trace()):
                    missing.append(f"{name} on {state}: a violation line")
    assert missing == []
    assert _violations(strict) == set()
    assert {state[0] for state in reached} == {"active", "passive", "quarantined", "evicted"}
    assert len(reached) == 11
    assert {t.id for t in HOST.transitions} - _hits(strict) == {"host.12"}


# ----------------------------------------------------------------- effect

_RUN_ID = "00000000-0000-4000-8000-000000000001"
_STRANGER = "00000000-0000-4000-8000-000000000002"


def _spawn(**fields: Any) -> Effect:
    return Effect(
        **{
            "effect_id": effect_id_for(1, "SPAWN", "j", 1),
            "kind": "SPAWN",
            "job": "j",
            "run_number": 1,
            "executor_id": LOCAL_EXECUTOR_ID,
            "index": 1,
            "at": T0,
            "run_id": _RUN_ID,
            "generation": 0,
            **fields,
        }
    )


#: the outbox calls an effect's state reaches, as the engine and
#: `read_outbox` make them: nothing, a record, or a record and an outcome
_EFFECT_BUILDS: dict[str, tuple[str, ...]] = {
    "absent": (),
    "pending": ("record",),
    "applied": ("record", "applied"),
    "retired": ("record", "retired"),
    "indeterminate": ("record", "indeterminate"),
}


def _effect_events() -> dict[str, Callable[[Outbox], None]]:
    """Every call the outbox accepts for one effect: the record again, a
    different record under its id, a KILL of its run naming another
    process, each outcome, and an outcome naming another process."""
    spawn = _spawn()
    kill = _spawn(effect_id=effect_id_for(2, "KILL", "j", 1), kind="KILL", index=2)
    events: dict[str, Callable[[Outbox], None]] = {
        "record": lambda box: box.record(spawn),
        "record other content": lambda box: box.record(_spawn(index=9)),
        "record a KILL naming a stranger": lambda box: box.record(
            kill.model_copy(update={"run_id": _STRANGER})
        ),
        "outcome naming a stranger": lambda box: box.resolve(
            EffectOutcome(effect_id=spawn.effect_id, state="applied", run_id=_STRANGER)
        ),
    }
    for outcome in ("applied", "retired", "indeterminate"):
        events[f"outcome {outcome}"] = _resolve_to(spawn.effect_id, outcome)
    return events


def _resolve_to(effect_id: str, outcome: str) -> Callable[[Outbox], None]:
    def resolve(box: Outbox) -> None:
        box.resolve(EffectOutcome(effect_id=effect_id, state=outcome))  # type: ignore[arg-type]

    return resolve


def test_every_outbox_call_in_every_effect_state_takes_a_declared_transition(
    strict: Path,
) -> None:
    """`effect`: all five states, built on an `Outbox` by its own verbs, as
    the engine and `read_outbox` build it (`_EFFECT_BUILDS`), and every
    call it accepts for that effect (`_effect_events`). A call takes a
    declared transition, is a no-op (the same record again), or is refused
    by the outbox's own EngineError before the table is asked. A second
    outcome is refused by the table itself, by design (DL-295): under the
    strict variable that refusal is the `TransitionError`, and it is the
    only one expected. Left out: nothing."""
    events = _effect_events()
    refused: set[tuple[str, str]] = set()
    for state, build in _EFFECT_BUILDS.items():
        for name, call in events.items():
            box = Outbox()
            for step in build:
                if step == "record":
                    box.record(_spawn())
                else:
                    box.resolve(EffectOutcome(effect_id=_spawn().effect_id, state=step))  # type: ignore[arg-type]
            assert (box.state_of(_spawn().effect_id) or "absent") == state
            try:
                call(box)
            except EngineError:
                assert (box.state_of(_spawn().effect_id) or "absent") == state
            except TransitionError:
                refused.add((state, name))
                assert box.state_of(_spawn().effect_id) == state
    second = {
        (s, f"outcome {o}") for s in EFFECT.finals for o in ("applied", "retired", "indeterminate")
    }
    assert refused == second
    outcomes = [t for t in EFFECT.transitions if t.trigger == "resolve"]
    assert len(outcomes) == 3
    assert _violations(strict) == {
        ("effect", t.id, state, str(t.target)) for state in EFFECT.finals for t in outcomes
    }
    assert {t.id for t in EFFECT.transitions} <= _hits(strict)


# ----------------------------------------------------------- subscription


def _feeds(path: Path) -> Iterator[tuple[Journal, Any]]:
    """Each feed state the journal and the handler reach, by their own
    calls: a new feed in backfill, live after the backfill, each with room
    or with a backlog its budget cannot grow, removed by an overflow from
    either, and closed from either."""
    for live in (False, True):
        for tight in (False, True):
            journal = Journal(path / f"j-{live}-{tight}.jsonl", fsync_each=False)
            feed = journal.subscribe(budget=1 if tight else 1 << 30)
            journal.preflight([])  # an empty backlog takes one record
            if live:
                feed.go_live()
            yield journal, feed
    for live in (False, True):
        for end in ("overflow", "unsubscribe"):
            journal = Journal(path / f"e-{live}-{end}.jsonl", fsync_each=False)
            feed = journal.subscribe(budget=1)
            journal.preflight([])
            if live:
                feed.go_live()
            if end == "overflow":
                journal.preflight([])
            else:
                journal.unsubscribe(feed)
            yield journal, feed


def test_every_feed_event_in_every_subscription_state_takes_a_declared_transition(
    strict: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`subscription`: `absent` (a new feed is `subscribe`, `subscription.01`)
    and every state a feed reaches through the journal and the handler
    (`_feeds`), and in each the events the journal applies to any feed: an
    append, and the stream's end (`unsubscribe`). In `backfill` also the
    backfill sent (`go_live`).

    Left out: `go_live` in `live`, `removed` and `closed`. The handler
    (`ControlServer._subscribe`) calls it once, after the ack and the
    backfill, with no await between the last backfill send and the call;
    an overflow cancels the handler, and the cancel lands at its next
    await, before `go_live`; and only the handler's `finally` ends the
    stream. So `go_live` meets only `backfill`."""
    states: set[str] = set()
    for journal, feed in _feeds(tmp_path):
        journal.close()
        states.add(feed.state)
    assert states == {"backfill", "live", "removed", "closed"}
    for event in ("append", "unsubscribe", "go_live"):
        (tmp_path / event).mkdir()
        for journal, feed in _feeds(tmp_path / event):
            try:
                if event == "append":
                    journal.preflight([])
                elif event == "unsubscribe":
                    journal.unsubscribe(feed)
                elif feed.state == "backfill":
                    feed.go_live()
            finally:
                journal.close()
    assert _violations(strict) == set()
    _no_violation_line(capsys.readouterr().err)
    assert {t.id for t in SUBSCRIPTION.transitions} <= _hits(strict)


# -------------------------------------------------------------- admission

#: the request id every admission case addresses. A client id: the
#: `engine:` prefix is the engine's own, and the envelope refuses it
_R = "r-1"
_G = "global:G"


def _set_global(value: str) -> Event:
    return Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": value})


#: (event, expect, epoch offset) of each operator command an admission case sends
type _Command = tuple[Event | HostCommand, dict[str, int], int]
_APPLIES: _Command = (_set_global("1"), {_G: 0}, 0)
_REJECTED: _Command = (_set_global("1"), {_G: 5}, 0)


def _send(engine: Engine, command: _Command) -> asyncio.Future[ApplyResult]:
    what, expect, epoch = command
    envelope = Envelope(request_id=_R, expect=expect, epoch=engine.epoch + epoch)
    if isinstance(what, HostCommand):
        return engine.submit_host(what, envelope)  # type: ignore[return-value]
    return engine.submit(what.model_copy(), envelope)  # type: ignore[return-value]


def _admission_events() -> dict[str, _Command | None]:
    """Every input that can address `_R`: the command that built the state
    (or, for an unseen id, one that applies), another command, a stale
    epoch, a failed precondition, a command that faults when applied, a
    host command, and (None) an input the engine makes. That one takes a name
    of its own, so it cannot address `_R`: it must leave the id where it was."""
    return {
        "the same command": None,
        "another command": (_set_global("2"), {_G: 0}, 0),
        "a stale epoch": (_set_global("1"), {_G: 0}, 1),
        "a failed precondition": (_set_global("3"), {_G: 7}, 0),
        "a faulting command": (Event(at=T0, kind="STARTJOB", payload={}), {"job:j": 0}, 0),
        "a host command": (
            HostCommand(verb="drain", host_id=LOCAL_EXECUTOR_ID),
            {"host:local": 1},
            0,
        ),
        "an engine-made input": None,
    }


async def _admission_case(state: str, event: str) -> tuple[str, str]:
    """Build `_R` in `state` at index 1, send `event`, and answer how it
    ended and the id's state after it."""
    engine = Engine(
        lower_source(_HOST_JIL), clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
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
            what, expect, _ = built
            assert isinstance(what, Event)
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
                        epoch=engine.epoch,
                        expect=expect,
                    ),
                )
            )
        assert engine.decisions.state(_R) == state
        command = _admission_events()[event]
        if event == "an engine-made input":
            engine.inject(_set_global("9"), source="scheduler")
            future = None
        else:
            future = _send(engine, built if command is None else command)
        try:
            await engine.run_until_quiescent(T0)
        except EngineError as exc:  # the loop's own stop: a second writer
            return f"stopped: {exc}", engine.decisions.state(_R)
        if future is None:
            return "made", engine.decisions.state(_R)
        try:
            return (await future).decision, engine.decisions.state(_R)
        except EngineError as exc:
            return f"refused {exc.code}", engine.decisions.state(_R)
    finally:
        await engine.shutdown()


def _wire_refuses_an_engine_name() -> bool:
    """The door: `parse_envelope` refuses a client request id that is one
    of the engine's own names, as an invalid argument."""
    request = {
        "v": PROTOCOL_VERSION,
        "baseline_id": "b",
        "request_id": "engine:2",
        "epoch": 1,
        "expect": {_G: 0},
    }
    try:
        parse_envelope(request, addressed=_G, baseline_id="b")
    except EnvelopeError as exc:
        return exc.code == "invalid_argument" and "engine:" in str(exc)
    return False


def test_every_input_in_every_request_id_state_takes_a_declared_transition(
    strict: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`admission`: one request id in each of its four states, built by a
    live engine (`unseen`, `applied`, `rejected`), and `admitted` as
    `read_decisions` leaves an attempt line with no decision (the engine
    itself never meets it at dedup: steps 5-7 do not yield to another
    input). Every input that can address the id (`_admission_events`) in
    each. An input takes a declared transition, or the engine's own rule
    refuses it (an AdmissionRefused answer, or the loop's stop on an
    admitted, undecided id).

    Left out: the two refusals made only while a seal is in flight
    (`period_sealing` comes before dedup and moves no id; a stamp behind
    the frontier during the seal's drain is refused from `unseen`, as the
    stale epoch is here); the seal's own request-id lookup
    (`boundary._check_request_id`, which takes the reused-id row of the
    id's state); and replay's moves (`read_decisions` notes every attempt
    and records every decision, `replay_inputs` records a recovered one),
    whose one collision the reserved `engine:` prefix closes
    (`test_a_client_request_id_in_the_engines_prefix_is_refused`)."""
    outcomes: dict[tuple[str, str], tuple[str, str]] = {}
    missing: set[tuple[str, str]] = set()
    for state in ("unseen", "applied", "rejected", "admitted"):
        for event in _admission_events():
            try:
                outcomes[(state, event)] = asyncio.run(_admission_case(state, event))
            except TransitionError:
                missing.add((state, event))
    assert missing == set()
    assert _wire_refuses_an_engine_name()
    assert _violations(strict) == set()
    _no_violation_line(capsys.readouterr().err)
    for state in ("unseen", "applied", "rejected", "admitted"):
        # an engine-made input has a name of its own and leaves the id alone
        assert outcomes[(state, "an engine-made input")] == ("made", state)
    assert outcomes[("unseen", "the same command")] == ("applied", "applied")
    assert outcomes[("applied", "the same command")] == ("applied", "applied")
    assert outcomes[("rejected", "a failed precondition")][0] == "refused request_id_reused"
    assert outcomes[("unseen", "a faulting command")] == ("refused apply_faulted", "unseen")
    assert outcomes[("admitted", "the same command")][0].startswith("stopped:")
    assert {t.id for t in ADMISSION.transitions} <= _hits(strict)


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
        "epoch": 1,
        "expect": {_G: 0},
    }
    if refused:
        with pytest.raises(EnvelopeError, match="reserves") as caught:
            parse_envelope(request, addressed=_G, baseline_id="b")
        assert caught.value.code == "invalid_argument"
    else:
        assert parse_envelope(request, addressed=_G, baseline_id="b").request_id == request_id
    # a request that addresses no row (a boundary) meets the same door
    boundary = {key: value for key, value in request.items() if key != "expect"}
    if refused:
        with pytest.raises(EnvelopeError, match="reserves"):
            parse_envelope(boundary, addressed=None, baseline_id="b")
    else:
        assert parse_envelope(boundary, addressed=None, baseline_id="b").request_id == request_id


def test_no_client_request_collides_with_an_engine_made_input_live_or_on_replay(
    strict: Path, capsys: pytest.CaptureFixture[str], short_root: Path
) -> None:
    """The collision the prefix closes, end to end. An operator tries the
    name the engine would give index 2, on `sendevent` and on `host`: both
    are refused on the wire and nothing reaches the log. A legal request
    takes index 1, the engine then makes input 2 and names it
    `engine:2`, and the client's decision stands: its retry is answered
    from it. Replay of the log notes every attempt without a violation."""
    from test_runner_control import _control_call, _read_revision, _serve, _teardown

    async def scenario() -> tuple[list[dict[str, Any]], bytes, list[dict[str, Any]]]:
        engine, server, loop_task = await _serve(short_root / "run", _HOST_JIL)
        try:
            assert engine.journal is not None
            baseline, epoch, revision = await _read_revision(server.path, _G)
            legal = command(
                "SET_GLOBAL",
                {"name": "G", "value": "1"},
                key=_G,
                revision=revision,
                baseline_id=baseline,
                epoch=epoch,
                request_id="r-1",
            )
            before = engine.journal.path.read_bytes()
            refusals = [
                await _control_call(server.path, {**legal, "request_id": "engine:2"}),
                await _control_call(
                    server.path,
                    command(
                        "drain",
                        {"id": LOCAL_EXECUTOR_ID},
                        key="host:local",
                        revision=1,
                        baseline_id=baseline,
                        epoch=epoch,
                        request_id="engine:2",
                        cmd="host",
                    ),
                ),
            ]
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
    assert _violations(strict) == set()
    _no_violation_line(capsys.readouterr().err)


def test_a_seal_request_cannot_take_an_engine_name(strict: Path, short_root: Path) -> None:
    """`seal` is a client request id too: the boundary refuses the prefix
    before it submits anything, and the engine stays open."""
    from test_boundary import C1_JIL, C2_JIL, _catalog, _seal_request_wire, _stage

    from dsl41.runner_clock import RealClock
    from dsl41.runner_control import ControlClient, ControlServer

    run_root = short_root / "run"
    catalog, sources = _catalog(C1_JIL)
    staged_manifest = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    engine = start_run(
        catalog,
        run_root,
        clock=RealClock(),
        adapters={"CMD": FakeAdapter(default=None)},
        hold_open=True,
        staged=staged_manifest,
    )

    async def scenario() -> dict[str, Any]:
        server = ControlServer(engine, run_root / "control.sock")
        await server.start()
        loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        client = ControlClient(run_root / "control.sock")
        try:
            staged = _stage(run_root, C2_JIL)
            answer = await client.request(_seal_request_wire(engine, staged, request_id="engine:7"))
            assert not loop_task.done()  # no boundary: the engine is still serving
            return answer
        finally:
            loop_task.cancel()
            await client.close()
            await server.close()
            await engine.shutdown()

    answer = asyncio.run(scenario())
    assert engine.journal is not None
    engine.journal.close()
    assert answer["ok"] is False and answer["refused"] is True
    assert answer["code"] == "invalid_argument" and "engine:" in answer["error"]


# ---------------------------------------------------------- seal_boundary

_C1_JIL = "insert_job: a\njob_type: c\ncommand: x\n\ninsert_job: b\njob_type: c\ncommand: y\n"
_C2_JIL = "insert_job: a\njob_type: c\ncommand: x\n\ninsert_job: b\njob_type: c\ncommand: z\n"


def _staged_manifest(run_root: Path, text: str) -> Any:
    parsed = [parse(text, file="estate.jil")]
    sources = [SourceFile(path="estate.jil", text=render_preserve(parsed[0]))]
    return lower_catalog(parsed), stage_manifest(
        lower_catalog(parsed),
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )


def _sealable(run_root: Path) -> tuple[Engine, SealRequest]:
    """A periodized root with period 1 open, and a request to seal it into
    a staged period 2, as `dsl41 run` and `dsl41 seal` make them."""
    catalog, manifest = _staged_manifest(run_root, _C1_JIL)
    engine = start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        staged=manifest,
    )
    staged: StagedNextPeriod = stage_next_period(
        run_root, staged_manifest=_staged_manifest(run_root, _C2_JIL)[1]
    )
    request = SealRequest(
        baseline_id=engine.baseline_id,
        epoch=engine.epoch,
        request_id="r-seal",
        next_period=staged,
        stage_digest=staged.stage_digest,
        force_seal=False,
        claimed_actor=_ACTOR,
    )
    return engine, request


def _seal_future(engine: Engine, request: SealRequest) -> asyncio.Future[Any]:
    """`submit_seal`'s answer, which is a future."""
    future = engine.submit_seal(request)
    assert isinstance(future, asyncio.Future)
    return future


def _lose_fence(engine: Engine) -> None:
    """Delete the lineage's anchor lock under the engine (PR-03)."""
    assert engine.estate is not None
    (default_anchor_dir(engine.estate.run_root) / "anchor.lock").unlink()


def _raise(exc: BaseException) -> Callable[..., Any]:
    def fault(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return fault


class _FailingEffectWrite:
    """The WAL file, except that writing an `effect_result` line fails
    after part of it may be on disk (DL-274)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def write(self, line: bytes) -> int:
        if b'"rec": "effect_result"' in line:
            raise OSError(28, "No space left on device")
        return int(self._inner.write(line))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _submit_second(engine: Engine, request: SealRequest, second: list[BaseException]) -> None:
    """Send another seal request while the boundary is in flight, and keep
    its answer."""
    refused = _seal_future(engine, request.model_copy(update={"request_id": "r-two"})).exception()
    assert refused is not None
    second.append(refused)


def _arrange(
    cell: str,
    engine: Engine,
    request: SealRequest,
    mp: pytest.MonkeyPatch,
    second: list[BaseException],
) -> SealRequest:
    """Put the fault for `cell` at the step of the boundary it names, by the
    path the code takes there, and answer the request to send. A second
    request sent during the boundary lands in `second`."""
    queued = Event(at=T0, kind="STARTJOB", payload={"job": "a"})
    match cell:
        case "requested: refused":
            return request.model_copy(update={"epoch": engine.epoch + 1})
        case "requested: fence lost":

            def lost(*_args: Any) -> Any:
                _lose_fence(engine)
                raise EngineError("readiness failed", code="seal_refused")

            mp.setattr(engine, "_readiness", lost)
        case "frozen: refused" | "frozen: fence lost":

            async def quiescence(_estate: Any) -> None:
                _submit_second(engine, request, second)
                if cell == "frozen: fence lost":
                    _lose_fence(engine)
                raise EngineError("not quiescent", code="seal_not_quiescent")

            mp.setattr(engine, "_await_quiescence", quiescence)
        case "frozen: an attempt applying":
            engine.inject(queued, source="scheduler")
            mp.setattr("dsl41.runner.apply_attempt", _raise(RuntimeError("apply fault")))
        case "frozen: an input unadmitted":
            engine.inject(
                queued.model_copy(update={"at": T0 - timedelta(minutes=1)}), source="adapter"
            )
        case "frozen: a WAL append unfinished":
            engine.inject(queued, source="scheduler")
            assert engine.journal is not None
            mp.setattr(engine.journal, "_f", _FailingEffectWrite(engine.journal._f))
        case "frozen: TransitionStop":
            engine.inject(queued, source="scheduler")
            mp.setattr(engine, "_stop_on_violation", _raise(TransitionStop("stop")))
        case "committing: the commit returns" | "committing: refused" | "committing: fence lost":

            def hook(stage: str) -> None:
                if stage != "after_sidecar":
                    return
                _submit_second(engine, request, second)
                if cell == "committing: fence lost":
                    _lose_fence(engine)
                if cell != "committing: the commit returns":
                    raise EngineError(f"crash at {stage}")

            mp.setattr(engine, "crash_point", hook)
        case "committing: past the point of no return":
            mp.setattr(
                engine,
                "crash_point",
                lambda stage: (
                    _raise(EngineError("crash"))() if stage == "after_seal_record" else None
                ),
            )
    return request


#: every (phase, event) the boundary's code can meet: the moves it takes
#: (the exit last), the phase it ends in, and for an in-doubt stop the
#: DL-274 note that names which flag stopped it. The phase is where the
#: event lands: `requested` until the barrier parks, `frozen` through the
#: drain, cutoff and quiescence, `committing` inside `commit_boundary`.
_FROZEN = ("seal_boundary.01",)
_COMMITTING = ("seal_boundary.01", "seal_boundary.02")
_SEAL_CELLS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "requested: refused": (("seal_boundary.04",), "aborted", ""),
    "requested: fence lost": (("seal_boundary.05",), "stopped", ""),
    "requested: cancelled": ((), "requested", ""),
    "frozen: refused": ((*_FROZEN, "seal_boundary.04"), "aborted", ""),
    "frozen: fence lost": ((*_FROZEN, "seal_boundary.05"), "stopped", ""),
    "frozen: an attempt applying": ((*_FROZEN, "seal_boundary.06"), "stopped", "not fully applied"),
    "frozen: an input unadmitted": ((*_FROZEN, "seal_boundary.06"), "stopped", "unadmitted"),
    "frozen: a WAL append unfinished": ((*_FROZEN, "seal_boundary.06"), "stopped", "WAL append"),
    "frozen: TransitionStop": ((*_FROZEN, "seal_boundary.08"), "stopped", ""),
    "frozen: cancelled": (_FROZEN, "frozen", ""),
    "committing: the commit returns": ((*_COMMITTING, "seal_boundary.03"), "sealed", ""),
    "committing: refused": ((*_COMMITTING, "seal_boundary.04"), "aborted", ""),
    "committing: fence lost": ((*_COMMITTING, "seal_boundary.05"), "stopped", ""),
    "committing: past the point of no return": (
        (*_COMMITTING, "seal_boundary.07"),
        "stopped",
        "",
    ),
}


async def _seal_cell(
    cell: str, run_root: Path, mp: pytest.MonkeyPatch, second: list[BaseException]
) -> tuple[tuple[str, ...], str, str, Engine]:
    """Run one cell. Answers the moves taken, the phase the boundary ended
    in, the notes on the exception that ended the loop, and the engine."""
    taken: list[str] = []
    real_move = runner._seal_move

    def spy(pending: _PendingSeal, t: Any, new: Any) -> None:
        taken.append(t.id)
        real_move(pending, t, new)

    mp.setattr(runner, "_seal_move", spy)
    engine, request = _sealable(run_root)
    if cell == "frozen: an input unadmitted":
        # the frontier at T0, so an input stamped before it cannot be admitted
        engine.inject(_set_global("0"), source="scheduler")
        await engine.run_until_quiescent(T0)
    request = _arrange(cell, engine, request, mp, second)
    parked = asyncio.Event()
    if cell == "frozen: cancelled":

        async def quiescence(_estate: Any) -> None:
            parked.set()
            await asyncio.Event().wait()  # a shutdown arrives while it waits

        mp.setattr(engine, "_await_quiescence", quiescence)
    if cell == "requested: cancelled":
        real_settle = engine._settle

        async def settle() -> None:
            # the loop's first await, before it takes the queued boundary
            await real_settle()
            if engine._seal is not None and engine._seal.phase == "requested":
                parked.set()
                await asyncio.Event().wait()  # a shutdown arrives while it waits

        mp.setattr(engine, "_settle", settle)
    future = _seal_future(engine, request)
    pending = engine._seal
    assert isinstance(pending, _PendingSeal) and pending.phase == "requested"
    if cell == "requested: refused":
        _submit_second(engine, request, second)  # while the first is still queued
    loop = asyncio.create_task(engine.run_until_quiescent(T0 + timedelta(minutes=1)))
    if cell in ("requested: cancelled", "frozen: cancelled"):
        # Wait for the pause point OR the loop's end, bounded, so a change that
        # lets the loop finish first fails the cell instead of hanging it.
        waiter = asyncio.ensure_future(parked.wait())
        await asyncio.wait({waiter, loop}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        assert parked.is_set(), "the loop never reached the cell's pause point"
        assert pending.phase == cell.split(":")[0]
        loop.cancel()
    notes = ""
    try:
        await loop
    except TransitionError:
        raise
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001 -- each exit is checked by its moves
        notes = " ".join(getattr(exc, "__notes__", []))
    if pending.phase in ("aborted", "sealed"):
        assert future.done()
    if future.done() and not future.cancelled():
        future.exception()  # read, so a refusal is not logged as never retrieved
    return tuple(taken), pending.phase, notes, engine


def test_every_exit_in_every_seal_phase_takes_a_declared_transition(
    strict: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`seal_boundary`: one boundary per cell, on a live engine leading a
    lineage, with the event put at the step of the boundary where its phase
    holds, by the path the code takes there (`_arrange`): a refusal, a
    fence loss, an attempt admitted and not applied, an engine-made input
    the drain cannot admit, an unfinished WAL append, the stop run option,
    a shutdown, the commit, and a failure past the point of no return. In
    each phase that has one, a second seal request too, which is refused
    `seal_in_flight` and moves nothing. Each exit takes its declared
    transition, or (a shutdown, while queued or frozen) none.

    Left out, because the code cannot meet them: in `requested`, every exit
    but a refusal, a fence loss and a shutdown while the boundary is still
    queued (`_run_boundary` neither awaits nor admits before the barrier
    parks, and the stop option, the in-doubt flags and BoundaryFailStop
    come from the drain or the commit); in
    `committing`, a shutdown, the stop option and the in-doubt flags
    (`commit_boundary` is synchronous and admits nothing, and the seal
    record is not a `_write`); in `frozen`, BoundaryFailStop and the
    commit. The finals have no events: the boundary is over and a new
    request is a new boundary."""
    ended: dict[str, tuple[tuple[str, ...], str, str]] = {}
    second_in: set[str] = set()
    for index, (cell, (_moves, _phase, note)) in enumerate(_SEAL_CELLS.items()):
        second: list[BaseException] = []
        with pytest.MonkeyPatch.context() as mp:
            taken, phase, notes, engine = asyncio.run(
                _seal_cell(cell, tmp_path / f"run{index}", mp, second)
            )
        ended[cell] = (taken, phase, note if note in notes else notes)
        for refusal in second:
            assert isinstance(refusal, EngineError) and refusal.code == "seal_in_flight"
            second_in.add(str(refusal).split("r-seal, ")[1].split(")")[0])
        if engine.journal is not None:
            engine.journal.close()
    assert ended == _SEAL_CELLS
    assert second_in == {"requested", "frozen", "committing"}
    assert _violations(strict) == set()
    _no_violation_line(capsys.readouterr().err)
    assert {t.id for t in SEAL_BOUNDARY.transitions} <= _hits(strict)
