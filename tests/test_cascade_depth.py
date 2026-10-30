"""A long instant cascade runs, and one that never ends stops.

The oracle's cascade is the executor's worklist, iterative: it takes no
frame per nested start, so the frames it needs do not grow with the job count
(DL-350). Only a fact's own recursion, such as a deep condition, can pass the
recursion limit, which the oracle raises to a fixed headroom
(`oracle.RECURSION_LIMIT`, DL-351); the error names the RecursionError and
the root's job. The hook counts a root dispatch's items and stops it at the
item cap for the estate's job count (`oracle.item_cap`, DL-351), with the
same error class and the same operator outcome: an engine-made input is
quarantined, a control input is refused `apply_faulted`.

The scale tests run the CLI in a subprocess: a C stack overflow would kill
the process, and it must not take pytest down with it.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Any

import pytest
from typer.testing import CliRunner

from dsl41 import oracle, runner
from dsl41.cli import app
from dsl41.equiv import equivalent_tier_c
from dsl41.ir import CatalogIR, lower_source
from dsl41.oracle import (
    ITEM_CEILING,
    ITEM_FLOOR,
    ITEMS_PER_JOB_SQUARED,
    RECURSION_LIMIT,
    Oracle,
    item_cap,
)
from dsl41.oracle_state import CascadeDepthError, Event, OracleError
from dsl41.run_root import catalog_hash_v2
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import AdmissionRefused, Envelope
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_startup import start_run

START = "2026-01-05T09:00:00"
T0 = datetime.fromisoformat(START)
TICK = 'date_conditions: 1\ndays_of_week: all\nstart_times: "09:05"\n'
JOBS = 300


def _box(name: str, condition: str | None = None, extra: str = "") -> str:
    cond = f"condition: {condition}\n" if condition else ""
    return f"insert_job: {name}\njob_type: b\n{cond}{extra}\n"


def _cmd(name: str, condition: str | None = None, extra: str = "") -> str:
    cond = f"condition: {condition}\n" if condition else ""
    return f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n{cond}{extra}\n"


def _box_chain(n: int) -> tuple[str, list[dict[str, object]]]:
    """Empty boxes, each started by the one before: one nested start per job.
    A box with no member completes at its start (DL-304)."""
    return _box("q0", extra=TICK) + "".join(_box(f"q{i}", f"s(q{i - 1})") for i in range(1, n)), []


def _box_ring(n: int) -> tuple[str, list[dict[str, object]]]:
    """A ring of empty boxes entered from `q0`. `q1` starts again on each
    move of the last box while `s(q0)` holds, so the cascade goes round the
    ring twice, nested, until DL-304's guard refuses `q1`."""
    jil = _box("q0", extra=TICK) + _box("q1", f"s(q0) | s(q{n - 1})")
    return jil + "".join(_box(f"q{i}", f"s(q{i - 1})") for i in range(2, n)), []


def _noexec_chain(n: int) -> tuple[str, list[dict[str, object]]]:
    """`q0` runs and completes; the jobs after it are bypassed one inside the
    next (SEM-22)."""
    jil = _cmd("q0", extra=TICK) + "".join(_cmd(f"q{i}", f"s(q{i - 1})") for i in range(1, n))
    noexec: list[dict[str, object]] = [
        {"at": START, "kind": "ON_NOEXEC", "payload": {"job": f"q{i}"}} for i in range(1, n)
    ]
    return jil, noexec


SHAPES = {"box-chain": _box_chain, "box-ring": _box_ring, "noexec-chain": _noexec_chain}


def _dsl41(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "faulthandler", "-m", "dsl41", *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_sem10_a_300_job_instant_cascade_runs_and_replays_in_a_fresh_process(
    shape: str, tmp_path: Path
) -> None:
    """DL-304: the three deepest shapes, 300 jobs each, started by an
    engine-made input (a scheduler tick, or a completion). At the default
    limit of 1000 a recursive cascade raised at about 45, 91 and 142 jobs
    (DL-306). The oracle's cascade takes no frame per start, so `rehearse`
    runs them and a fresh process replays them for `journal`, neither fitting
    the limit to the job count. Neither raises nor dies on a signal."""
    jil, events = SHAPES[shape](JOBS)
    (tmp_path / "estate.jil").write_text(jil)
    (tmp_path / "scenario.json").write_text(json.dumps({"events": events}))
    root = tmp_path / "run"
    played = _dsl41(
        "rehearse",
        str(tmp_path / "estate.jil"),
        "--scenario",
        str(tmp_path / "scenario.json"),
        "--start",
        START,
        "--hours",
        "1",
        "--format",
        "json",
        "--run-root",
        str(root),
    )
    assert played.returncode == 0, played.stderr[-2000:]
    jobs = json.loads(played.stdout)["jobs"]
    assert len(jobs) == JOBS
    assert {row["final_status"] for row in jobs.values()} == {"SUCCESS"}
    if shape == "box-ring":
        assert jobs[f"q{JOBS - 1}"]["runs"] == 2
        trace = json.loads(played.stdout)["trace"]
        assert any(t["job"] == "q1" and t["transition"] == "START_REFUSED" for t in trace)
    replayed = _dsl41("journal", str(root))
    assert replayed.returncode == 0, replayed.stderr[-2000:]
    successes = {line.split()[1] for line in replayed.stdout.splitlines() if "->SUCCESS" in line}
    assert successes == set(jobs)


def _depth() -> int:
    frame: FrameType | None = sys._getframe()
    depth = 0
    while frame is not None:
        frame, depth = frame.f_back, depth + 1
    return depth


@pytest.mark.usefixtures("own_recursion_limit")
def test_sem10_a_cascade_past_the_limit_names_its_first_start() -> None:
    """When a fact's own recursion, a deep condition, passes the limit, the
    error says RecursionError and names the job whose input began the
    cascade, not the frame that broke. It is still a RecursionError, and an
    OracleError, so a caller that reports oracle errors reports it."""
    built = Oracle(lower_source(_deep_jil(400)))
    sys.setrecursionlimit(_depth() + 300)
    with pytest.raises(CascadeDepthError) as raised:
        built.feed(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "a"}))
    assert isinstance(raised.value, RecursionError)
    assert isinstance(raised.value, OracleError)
    assert raised.value.job == "a"
    # wrapped once, at the root dispatch
    assert type(raised.value.__cause__) is RecursionError
    assert str(raised.value).startswith("RecursionError: an instant cascade passed")
    assert "its first start was a (FORCE_STARTJOB event)" in str(raised.value)


def _force_q0() -> Event:
    return Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "q0"})


def _running_box_fold(jobs: int) -> tuple[str, list[Event]]:
    """Boxes `q1`.. each hold one ON_NOEXEC member `p<i>` waiting on the box
    before. Every box is force-started first, so it runs with its member
    unmet. Then the forced start of the empty `q0` bypasses `p1`, which
    folds the running `q1` to SUCCESS, which bypasses `p2`, and so on: each
    level completes a run that began before the cascade."""
    levels = (jobs - 1) // 2
    jil = _box("q0") + "".join(
        _box(f"q{i}") + f"insert_job: p{i}\njob_type: c\ncommand: x\nmachine: m1\n"
        f"box_name: q{i}\ncondition: s(q{i - 1})\n\n"
        for i in range(1, levels + 1)
    )
    setup = [Event(at=T0, kind="ON_NOEXEC", payload={"job": f"p{i}"}) for i in range(1, levels + 1)]
    setup += [
        Event(at=T0, kind="FORCE_STARTJOB", payload={"job": f"q{i}"}) for i in range(levels, 0, -1)
    ]
    return jil, setup


def _terminator_chain(jobs: int) -> tuple[str, list[Event]]:
    """Boxes nested one inside the next, each a job_terminator of the box
    around it, a command job innermost. All run; then the kill of `q0`
    terminates them in the box ends' cascade, one kill inside the next
    (SEM-14, DL-343). The kill nests no start: the slope is the
    terminator cascade's own."""
    jil = _box("q0") + "".join(
        _box(f"q{i}", extra=f"box_name: q{i - 1}\njob_terminator: 1\n") for i in range(1, jobs - 1)
    )
    jil += _cmd(f"q{jobs - 1}", extra=f"box_name: q{jobs - 2}\njob_terminator: 1\n")
    return jil, [_force_q0()]


def _release_wake_chain(jobs: int) -> tuple[str, list[Event]]:
    """The box chain, begun by a readmission: `h` keeps its unit after its
    run, so `q0`, which needs it, waits in the queue; RELEASE_RESOURCE on `h`
    wakes the queue, and `q0`'s readmission starts it and the chain
    (DL-345)."""
    jil = "insert_resource: R\nres_type: R\namount: 1\n\n"
    jil += _cmd("h", extra="resources: (R, QUANTITY=1, FREE=N)\n")
    jil += _box("q0", extra="resources: (R, QUANTITY=1, FREE=Y)\n")
    jil += "".join(_box(f"q{i}", f"s(q{i - 1})") for i in range(1, jobs - 1))
    setup = [
        Event(at=T0, kind="STARTJOB", payload={"job": "h"}),
        Event(at=T0, kind="STATUS", payload={"job": "h", "status": "SUCCESS"}),
        Event(at=T0, kind="STARTJOB", payload={"job": "q0"}),
    ]
    return jil, setup


SLOPE_SHAPES = {
    "box-chain": lambda jobs: (_box_chain(jobs)[0], []),
    "box-ring": lambda jobs: (_box_ring(jobs)[0], []),
    "running-box-fold": _running_box_fold,
    "terminator-chain": _terminator_chain,
    "release-wake-chain": _release_wake_chain,
}
#: The input that runs a shape's cascade, and the status it leaves every job
#: in; a forced start of `q0` and SUCCESS unless named here.
_LAST: dict[str, tuple[Event, str]] = {
    "terminator-chain": (Event(at=T0, kind="KILLJOB", payload={"job": "q0"}), "TERMINATED"),
    "off-hold-chain": (Event(at=T0, kind="OFF_HOLD", payload={"job": "q0"}), "SUCCESS"),
    "noexec-box-chain": (Event(at=T0, kind="ON_NOEXEC", payload={"job": "q0"}), "SUCCESS"),
    "release-wake-chain": (
        Event(at=T0, kind="RELEASE_RESOURCE", payload={"job": "h"}),
        "SUCCESS",
    ),
}


def _off_hold_chain(jobs: int) -> tuple[str, list[Event]]:
    """The box chain, begun by OFF_HOLD's retry of a held `q0` (SEM-21,
    DL-344)."""
    jil = _box("q0") + "".join(_box(f"q{i}", f"s(q{i - 1})") for i in range(1, jobs))
    return jil, [Event(at=T0, kind="ON_HOLD", payload={"job": "q0"})]


def _noexec_box_chain(jobs: int) -> tuple[str, list[Event]]:
    """The box chain, begun by ON_NOEXEC on a held `q0` with one member `p`
    (DL-344): the flag clears the hold, so the box's retry starts it, its
    member bypasses (SEM-22) and its SUCCESS starts `q1`."""
    jil = _box("q0") + _cmd("p", extra="box_name: q0\n")
    jil += "".join(_box(f"q{i}", f"s(q{i - 1})") for i in range(1, jobs - 1))
    return jil, [Event(at=T0, kind="ON_HOLD", payload={"job": "q0"})]


def _paren_chain(jobs: int) -> tuple[str, list[Event]]:
    """The box chain with each condition 30 parentheses deep: the deepest
    read of a fact at the innermost attempt."""
    wrap = "(" * 30 + "{} " + ")" * 30
    jil = _box("q0") + "".join(_box(f"q{i}", wrap.format(f"s(q{i - 1})")) for i in range(1, jobs))
    return jil, []


def _override_fold(jobs: int) -> tuple[str, list[Event]]:
    """The running-box fold, each box's box_success 30 parentheses deep over
    its member: the deepest read of a box rule's fact, the override of the
    innermost completion door (DL-346)."""
    jil, setup = _running_box_fold(jobs)
    wrap = "(" * 30 + "s(p{0}) " + ")" * 30
    for i in range(1, (jobs - 1) // 2 + 1):
        box = f"insert_job: q{i}\njob_type: b\n"
        jil = jil.replace(box, f"{box}box_success: {wrap.format(i)}\n")
    return jil, setup


#: Python's default recursion limit.
DEFAULT_LIMIT = 1000


def _frames_needed(shape: str, jobs: int) -> int:
    """The fewest frames above the caller that a forced start of `q0` needs
    to run the shape's cascade over `jobs` jobs, by bisection."""
    shapes = {
        **SLOPE_SHAPES,
        "paren-chain": _paren_chain,
        "override-fold": _override_fold,
        "off-hold-chain": _off_hold_chain,
        "noexec-box-chain": _noexec_box_chain,
    }
    jil, setup = shapes[shape](jobs)
    catalog = lower_source(jil)
    assert len(catalog.jobs) == jobs

    last, status = _LAST.get(shape, (_force_q0(), "SUCCESS"))

    def fits(frames: int) -> bool:
        built = Oracle(catalog)
        sys.setrecursionlimit(100_000)
        for event in setup:
            built.feed(event)
        sys.setrecursionlimit(_depth() + frames)
        try:
            built.feed(last)
        except RecursionError:
            return False
        finally:
            sys.setrecursionlimit(100_000)
        assert {row.status for row in built.store.job.values()} == {status}
        return True

    low, high = 1, DEFAULT_LIMIT
    while low < high:
        middle = (low + high) // 2
        if fits(middle):
            high = middle
        else:
            low = middle + 1
    return low


def _slope(shape: str) -> float:
    """The frames a cascade of the shape needs per job, between two sizes."""
    # box nesting stops at 64 levels (the lowering's bound)
    small, large = (21, 61) if shape == "terminator-chain" else (41, 81)
    needed = _frames_needed(shape, large) - _frames_needed(shape, small)
    return needed / (large - small)


_FRAME_SHAPES = [
    *sorted(SLOPE_SHAPES),
    "paren-chain",
    "override-fold",
    "off-hold-chain",
    "noexec-box-chain",
]


@pytest.mark.usefixtures("own_recursion_limit")
@pytest.mark.parametrize("shape", _FRAME_SHAPES)
def test_sem10_the_oracle_needs_no_frame_per_job(shape: str) -> None:
    """DL-350: the oracle's cascade is the executor's worklist, popped in a
    loop, so a nested start, a box rule or a wake takes no Python frame: the
    frames a cascade needs do not grow with the job count, on every shape.
    What it needs is a constant, a deep condition's own recursion its largest
    part, well inside Python's default limit."""
    assert _slope(shape) == 0
    assert _frames_needed(shape, 41) < DEFAULT_LIMIT // 4


@pytest.mark.usefixtures("own_recursion_limit")
def test_sem10_equiv_tier_c_runs_a_long_cascade_without_a_fit() -> None:
    """equiv's tier (c) runs scripts through the oracle, so a script can
    start a cascade as long as a live one; the limit is the oracle's fixed
    headroom, whatever the job count."""
    large = lower_source(_box_chain(JOBS)[0])
    sys.setrecursionlimit(1000)
    result = equivalent_tier_c(large, large, [[_force_q0()]])
    assert result.equivalent
    assert sys.getrecursionlimit() == RECURSION_LIMIT


# ------------------------------------------------------------- what lowers evaluates


def _deep_jil(depth: int) -> str:
    grouped = "(" * depth + "s(a)" + ")" * depth
    return _cmd("a") + _cmd("b", grouped)


def _deepest_lowered() -> CatalogIR:
    """The catalog whose condition is the deepest the lowering accepts at
    Python's default limit, found from a fresh thread's stack, where the
    lowering reaches deepest (DL-20)."""
    found: dict[str, Any] = {}

    def search() -> None:
        sys.setrecursionlimit(1000)
        low, high = 1, 2000
        while low < high:
            middle = (low + high + 1) // 2
            try:
                lower_source(_deep_jil(middle))
            except Exception:  # noqa: BLE001 -- the lowering's refusal, whatever its class
                high = middle - 1
            else:
                low = middle
        found["depth"] = low
        found["catalog"] = lower_source(_deep_jil(low))

    thread = threading.Thread(target=search)
    thread.start()
    thread.join()
    assert 900 < found["depth"] < 1000
    catalog: CatalogIR = found["catalog"]
    return catalog


def _engine_wakes_b(catalog: CatalogIR) -> str:
    """`a`'s forced start through an engine with no run root, at the engine's
    own call site: `b`'s status once the engine is quiet."""

    async def scenario() -> str:
        engine = Engine(catalog, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()})
        engine.inject(_event("FORCE_STARTJOB", "a"))
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        status = engine.oracle.store.job["b"].status
        await engine.shutdown()
        return status

    return asyncio.run(scenario())


@pytest.mark.usefixtures("own_recursion_limit")
def test_sem10_the_deepest_condition_that_lowers_evaluates_at_the_engine_s_call_site() -> None:
    """What lowers must evaluate: at Python's default limit the engine's oracle
    raises it to `RECURSION_LIMIT`, so `a`'s completion wakes `b`, whose
    condition is grouped as deep as the lowering accepts."""
    catalog = _deepest_lowered()
    sys.setrecursionlimit(1000)
    assert _engine_wakes_b(catalog) in ("STARTING", "RUNNING", "SUCCESS")


@pytest.mark.usefixtures("own_recursion_limit")
def test_sem10_without_the_headroom_the_deepest_condition_does_not_evaluate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: at the default limit the same wake meets the depth
    error, so the headroom is what lets it evaluate."""
    catalog = _deepest_lowered()
    monkeypatch.setattr(oracle, "RECURSION_LIMIT", 1000)
    sys.setrecursionlimit(1000)
    assert _engine_wakes_b(catalog) == "INACTIVE"


def _deepest_stored() -> int:
    """The deepest grouping a run root can store: its catalog hash serializes
    the conditions, and pydantic refuses a nesting deeper than its own bound,
    whatever the recursion limit."""
    low, high = 1, 1000
    while low < high:
        middle = (low + high + 1) // 2
        try:
            catalog_hash_v2(lower_source(_deep_jil(middle)))
        except EngineError:
            high = middle - 1
        else:
            low = middle
    return low


@pytest.mark.usefixtures("own_recursion_limit")
def test_sem10_a_run_root_stores_no_deeper_condition_than_its_replay_evaluates(
    tmp_path: Path,
) -> None:
    """A run root holds only what its catalog hash serializes, far less deep
    than the lowering accepts. The deepest it stores runs in the engine and
    replays from the default limit, so no logged input carries a condition
    its replay cannot evaluate."""
    depth = _deepest_stored()
    # pydantic's bound: 252 in a fresh process, one more after some suites
    assert 240 <= depth <= 260
    sys.setrecursionlimit(1000)

    (tmp_path / "estate.jil").write_text(_deep_jil(depth))
    scenario = {"events": [{"at": START, "kind": "FORCE_STARTJOB", "payload": {"job": "a"}}]}
    (tmp_path / "scenario.json").write_text(json.dumps(scenario))
    root = tmp_path / "run"
    invoke = CliRunner().invoke
    played = invoke(
        app,
        [
            "rehearse",
            str(tmp_path / "estate.jil"),
            "--scenario",
            str(tmp_path / "scenario.json"),
            "--start",
            START,
            "--hours",
            "1",
            "--run-root",
            str(root),
        ],
    )
    assert played.exit_code == 0, played.output
    sys.setrecursionlimit(1000)
    replayed = invoke(app, ["journal", str(root)])
    assert replayed.exit_code == 0, replayed.output
    assert "b INACTIVE->STARTING" in replayed.output
    assert "rejected input" not in replayed.output


#: A grouping past the catalog hash's bound, whatever the process ran before.
_PAST_THE_HASH = 300


def test_sem10_a_condition_deeper_than_the_hash_serializes_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """A condition grouped past the serializer's bound (about 250 levels) lowers,
    and the catalog hash refuses it with a named error, not the serializer's
    traceback; rehearse prints the refusal and exits 2."""
    catalog = lower_source(_deep_jil(_PAST_THE_HASH))
    with pytest.raises(EngineError, match="deeper than its hash serializes \\(about 250 levels"):
        catalog_hash_v2(catalog)
    (tmp_path / "estate.jil").write_text(_deep_jil(_PAST_THE_HASH))
    played = CliRunner().invoke(
        app, ["rehearse", str(tmp_path / "estate.jil"), "--start", START, "--hours", "1"]
    )
    assert played.exit_code == 2, played.output
    assert "deeper than its hash serializes" in played.output
    assert isinstance(played.exception, SystemExit)


def test_sem10_the_hash_names_only_the_depth_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any other error out of the serializer leaves the hash as it was."""
    catalog = lower_source(_deep_jil(1))

    def broken(*_args: object, **_kwargs: object) -> object:
        raise ValueError("some other serializer error")

    monkeypatch.setattr(type(catalog), "model_dump", broken)
    with pytest.raises(ValueError, match="some other serializer error"):
        catalog_hash_v2(catalog)


# ------------------------------------------------------------- the item cap


def test_sem10_the_item_cap_is_a_floor_a_square_then_a_ceiling() -> None:
    """The floor to 158 jobs, four times the square to 316, the ceiling above."""
    assert item_cap(1) == item_cap(158) == ITEM_FLOOR == 100_000
    assert item_cap(159) == ITEMS_PER_JOB_SQUARED * 159**2 == 101_124
    assert item_cap(200) == 160_000
    assert item_cap(316) == ITEMS_PER_JOB_SQUARED * 316**2 == 399_424
    assert item_cap(317) == item_cap(400) == item_cap(1000) == ITEM_CEILING == 400_000


class _Counting(Oracle):
    """The oracle, keeping the most items any root dispatch took."""

    peak = 0

    def _check_reached(self) -> None:
        self.peak = max(self.peak, self._items)
        super()._check_reached()


def _event(kind: str, job: str, **payload: object) -> Event:
    return Event(at=T0, kind=kind, payload={"job": job, **payload})


def _queue_scan(jobs: int) -> tuple[str, list[Event]]:
    """Half the waiters queue on R2, which `h2` holds, ahead of the other half
    on R1, all of whose units `h1` keeps after its run. RELEASE_RESOURCE on `h1`
    frees them, and each of the scan's passes readmits every blocked waiter
    before it admits one more: about half the square of the jobs in items."""
    half = (jobs - 2) // 2
    jil = f"insert_resource: R1\nres_type: R\namount: {half}\n\n"
    jil += "insert_resource: R2\nres_type: R\namount: 1\n\n"
    jil += _cmd("h1", extra=f"resources: (R1, QUANTITY={half}, FREE=N)\n")
    jil += _cmd("h2", extra="resources: (R2, QUANTITY=1, FREE=Y)\n")
    jil += "".join(
        _cmd(f"w{i}", extra="resources: (R2, QUANTITY=1, FREE=Y)\n") for i in range(half)
    )
    jil += "".join(
        _cmd(f"v{i}", extra="resources: (R1, QUANTITY=1, FREE=Y)\n") for i in range(half)
    )
    setup = [
        _event("STARTJOB", "h1"),
        _event("STATUS", "h1", status="SUCCESS"),
        _event("STARTJOB", "h2"),
        *(_event("STARTJOB", f"w{i}") for i in range(half)),
        *(_event("STARTJOB", f"v{i}") for i in range(half)),
    ]
    return jil, setup


def _fan_in(jobs: int) -> tuple[str, list[Event]]:
    """A chain of empty boxes, and as many command jobs each waiting on all of
    them: every box's completion wakes every waiter, about 0.8 times the
    square of the jobs in items."""
    half = jobs // 2
    jil = _box("q0") + "".join(_box(f"q{i}", f"s(q{i - 1})") for i in range(1, half))
    every = " & ".join(f"s(q{i})" for i in range(half))
    return jil + "".join(_cmd(f"c{j}", every) for j in range(half)), []


_CAP_SHAPES = {
    **SLOPE_SHAPES,
    "paren-chain": _paren_chain,
    "override-fold": _override_fold,
    "off-hold-chain": _off_hold_chain,
    "noexec-box-chain": _noexec_box_chain,
    "queue-scan": _queue_scan,
    "fan-in": _fan_in,
}
_CAP_LAST = {"queue-scan": _event("RELEASE_RESOURCE", "h1")}


def _peak(shape: str, jobs: int) -> tuple[int, int]:
    """The most items a root dispatch of the shape took over `jobs` jobs,
    its setup included, and the estate's job count."""
    jil, setup = _CAP_SHAPES[shape](jobs)
    catalog = lower_source(jil)
    built = _Counting(catalog)
    last = _CAP_LAST.get(shape) or _LAST.get(shape, (_force_q0(), "SUCCESS"))[0]
    for event in [*setup, last]:
        built.feed(event)
    return built.peak, len(catalog.jobs)


#: The least margin the cap keeps over a legitimate shape (4.7 measured, the
#: fan-in at 200 jobs).
MARGIN = 4.5


@pytest.mark.parametrize(
    ("shape", "jobs"),
    [
        *((shape, jobs) for shape in sorted(_CAP_SHAPES) for jobs in (41, 64, 100)),
        ("box-ring", 200),
        ("queue-scan", 200),
        ("fan-in", 200),
    ],
)
def test_sem10_the_item_cap_holds_the_largest_shapes_with_its_margin(shape: str, jobs: int) -> None:
    """DL-304's shapes, a queue scan and a fan-in of wakes, the largest
    legitimate cascades measured, take at most the cap's `MARGIN`th part, at
    the floor and where the square binds; the nested terminator chain stops at
    the lowering's 64 levels."""
    if shape == "terminator-chain" and jobs > 64:
        pytest.skip("box nesting stops at 64 levels")
    peak, count = _peak(shape, jobs)
    assert MARGIN * peak <= item_cap(count)
    # the two square shapes take what their docstrings say, so the margin is not vacuous
    assert peak > {"queue-scan": 0.5, "fan-in": 0.8}.get(shape, 0) * count**2


def _loop_estate(jobs: int) -> CatalogIR:
    """DL-348's shape: a box whose condition is its own end, holding a member
    with both terminators, and idle jobs to make up the estate."""
    jil = _box("bx", "t(bx)")
    jil += _cmd("k", extra="box_name: bx\nbox_terminator: 1\njob_terminator: 1\n")
    jil += "".join(_cmd(f"f{i}") for i in range(jobs - 2))
    return lower_source(jil)


class _BeforeDL348(Oracle):
    """The box's end reaches every member, whatever run the box is in: the
    defect DL-348 fixed, which loops on `_loop_estate` with no nested start."""

    def _still_the_run(self, box: str, run: int) -> bool:
        return True


def test_sem10_a_cascade_that_never_ends_stops_at_the_item_cap() -> None:
    """The kill of `bx` terminates it; its end starts it again, and the end's
    kill of `k`, carried into the new run, ends that run, and so on, each
    start a sibling of the last, so job.100 never fires. At 200 jobs the
    hook stops it at the cap with the depth error, naming the cap, the job
    count and the root item, and the oracle takes its next input."""
    built = _BeforeDL348(_loop_estate(200))
    built.feed(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "bx"}))
    with pytest.raises(CascadeDepthError) as raised:
        built.feed(Event(at=T0, kind="KILLJOB", payload={"job": "bx"}))
    assert isinstance(raised.value, RecursionError)
    assert isinstance(raised.value, OracleError)
    assert raised.value.job == "bx"
    assert str(raised.value) == (
        "an instant cascade reached the item cap of 160000 for an estate of 200 jobs;"
        " its root item was KILLJOB on bx"
    )
    built.feed(Event(at=T0, kind="STATUS", payload={"job": "f0", "status": "SUCCESS"}))
    assert built.store.job["f0"].status == "SUCCESS"


def _capped(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    monkeypatch.setattr(oracle, "item_cap", lambda _jobs: cap)


def _chain_items(jobs: int) -> int:
    """The items of the forced start of a box chain over `jobs` jobs: one
    root dispatch."""
    built = _Counting(lower_source(_box_chain(jobs)[0]))
    built.feed(_force_q0())
    return built.peak


def test_sem10_a_dispatch_below_the_cap_runs_and_one_at_the_cap_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is the first item count a root dispatch may not reach."""
    items = _chain_items(20)
    catalog = lower_source(_box_chain(20)[0])
    _capped(monkeypatch, items + 1)
    below = Oracle(catalog)
    below.feed(_force_q0())
    assert {row.status for row in below.store.job.values()} == {"SUCCESS"}
    _capped(monkeypatch, items)
    at = Oracle(catalog)
    with pytest.raises(CascadeDepthError, match=f"item cap of {items} for an estate of 20 jobs"):
        at.feed(_force_q0())


def test_sem10_a_control_input_at_the_cap_is_refused_apply_faulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control input whose dry apply reaches the cap is refused before the
    WAL, as any control input whose apply raises (DL-292)."""
    _capped(monkeypatch, 50)

    async def scenario() -> AdmissionRefused:
        # the chain without `q0`'s tick: this engine wires no scheduler
        jil = _box("q0") + "".join(_box(f"q{i}", f"s(q{i - 1})") for i in range(1, 20))
        engine = start_run(
            lower_source(jil),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
        )
        future = engine.submit(
            _force_q0(), Envelope(request_id="r1", expect={}, epoch=engine.epoch)
        )
        assert isinstance(future, asyncio.Future)
        await engine.run_until_quiescent(T0)
        with pytest.raises(AdmissionRefused) as refused:
            await future
        assert engine.oracle.store.job["q0"].status == "INACTIVE"
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return refused.value

    refusal = asyncio.run(scenario())
    assert refusal.code == "apply_faulted"
    assert "an instant cascade reached the item cap of 50 for an estate of 20 jobs" in str(refusal)


def test_sem10_an_engine_made_input_at_the_cap_is_quarantined_and_replays_alike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine whose cap the scheduler's tick reaches meets the error on the
    tick's dry apply. The tick's time half applies alone, so the tick is
    quarantined: logged with its rejection, and rehearse names it and fails
    (concurrency-model ss4). A replay under the same cap quarantines it the
    same way; the cap is the catalog's, so the live engine and its replays
    agree. A log an older build wrote holds the tick with no decision, and a
    replay under the cap quarantines it too."""
    jil, _ = _box_chain(200)
    (tmp_path / "estate.jil").write_text(jil)
    _capped(monkeypatch, 500)
    invoke = CliRunner().invoke
    rehearse = ["rehearse", str(tmp_path / "estate.jil"), "--start", START, "--hours", "1"]
    capped = "an instant cascade reached the item cap of 500 for an estate of 200 jobs"
    quarantined = "rejected input 1 (STARTJOB for job q0 from scheduler): engine_apply_faulted:"
    root = tmp_path / "run"
    played = invoke(app, [*rehearse, "--run-root", str(root)])
    assert played.exit_code == 1, played.output
    assert "quarantined input 1: quarantined: applying STARTJOB for job q0" in played.output
    assert f"{capped}; its root item was STARTJOB (STARTJOB event (scheduler)) on q0" in (
        played.output
    )
    narrated = invoke(app, ["journal", str(root)])
    assert narrated.exit_code == 0, narrated.output
    assert quarantined in narrated.output
    assert "q199 RUNNING->SUCCESS" not in narrated.output
    # the older build: no dry apply for an engine-made input, so the tick
    # reached the WAL before its apply raised
    old = tmp_path / "old"
    with monkeypatch.context() as older:
        older.setattr(runner.Engine, "_dry_apply_engine_made", lambda _self, _attempt: None)
        died = invoke(app, [*rehearse, "--run-root", str(old)])
    assert died.exit_code == 1, died.output
    assert f"rehearse failed: {capped}" in died.output
    recovered = invoke(app, ["journal", str(old)])
    assert recovered.exit_code == 0, recovered.output
    assert quarantined in recovered.output
    assert f"CascadeDepthError: {capped}" in recovered.output
