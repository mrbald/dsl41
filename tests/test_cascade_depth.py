"""A long instant cascade fits the interpreter's recursion limit.

The oracle evaluates a cascade of instant starts recursively, one start
inside the next. An engine-made input is in the WAL before it is applied, so
a RecursionError there would stop every resume at the same input. The engine
and the replay fit the limit to the catalog's job count
(`oracle.fit_recursion_limit`); a cascade that still passes it raises an
error that names the RecursionError and the cascade's first start.

The scale tests run the CLI in a subprocess: a C stack overflow would kill
the process, and it must not take pytest down with it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import FrameType

import pytest
from typer.testing import CliRunner

from dsl41 import runner, runner_journal
from dsl41.cli import app
from dsl41.equiv import equivalent_tier_c
from dsl41.ir import lower_source
from dsl41.oracle import (
    FRAMES_PER_NESTED_START,
    MAX_NESTED_STARTS,
    RECURSION_LIMIT_BASE,
    Oracle,
    fit_recursion_limit,
)
from dsl41.oracle_state import CascadeDepthError, Event, OracleError

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
    limit of 1000 they raise at about 45, 91 and 142 jobs. The engine fits
    the limit in `rehearse`; a fresh process fits it again for `journal`'s
    replay. Neither raises nor dies on a signal."""
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


def test_sem10_the_recursion_limit_fits_the_job_count_and_never_drops() -> None:
    """Two nested starts per job at most, each at most
    `FRAMES_PER_NESTED_START` frames, over a base. A limit already higher is
    kept."""
    sys.setrecursionlimit(1000)
    fitted = fit_recursion_limit(JOBS)
    assert fitted == RECURSION_LIMIT_BASE + FRAMES_PER_NESTED_START * MAX_NESTED_STARTS * JOBS
    assert sys.getrecursionlimit() == fitted
    assert fit_recursion_limit(1) == fitted
    assert sys.getrecursionlimit() == fitted


def _depth() -> int:
    frame: FrameType | None = sys._getframe()
    depth = 0
    while frame is not None:
        frame, depth = frame.f_back, depth + 1
    return depth


def test_sem10_a_cascade_past_the_limit_names_its_first_start() -> None:
    """When a cascade does pass the limit, the error says RecursionError and
    names the job whose start began the cascade, not the frame that broke.
    It is still a RecursionError, and an OracleError, so a caller that
    reports oracle errors reports it."""
    jil, _ = _box_chain(100)
    oracle = Oracle(lower_source(jil))
    sys.setrecursionlimit(_depth() + 400)
    with pytest.raises(CascadeDepthError) as raised:
        oracle.feed(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "q0"}))
    assert isinstance(raised.value, RecursionError)
    assert isinstance(raised.value, OracleError)
    assert raised.value.job == "q0"
    # wrapped once, at the outermost start: no inner start wrapped it first
    assert type(raised.value.__cause__) is RecursionError
    assert str(raised.value).startswith("RecursionError: an instant cascade passed")
    assert "its first start was q0 (" in str(raised.value)


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


SLOPE_SHAPES = {
    "box-chain": lambda jobs: (_box_chain(jobs)[0], []),
    "box-ring": lambda jobs: (_box_ring(jobs)[0], []),
    "running-box-fold": _running_box_fold,
}


def _frames_needed(shape: str, jobs: int) -> int:
    """The fewest frames above the caller that a forced start of `q0` needs
    to run the shape's cascade over `jobs` jobs, by bisection."""
    jil, setup = SLOPE_SHAPES[shape](jobs)
    catalog = lower_source(jil)
    assert len(catalog.jobs) == jobs

    def fits(frames: int) -> bool:
        oracle = Oracle(catalog)
        for event in setup:
            oracle.feed(event)
        sys.setrecursionlimit(_depth() + frames)
        try:
            oracle.feed(_force_q0())
        except RecursionError:
            return False
        finally:
            sys.setrecursionlimit(100_000)
        assert {row.status for row in oracle.store.job.values()} == {"SUCCESS"}
        return True

    low, high = 1, 4 * FRAMES_PER_NESTED_START * jobs
    while low < high:
        middle = (low + high) // 2
        if fits(middle):
            high = middle
        else:
            low = middle + 1
    return low


@pytest.mark.parametrize("shape", sorted(SLOPE_SHAPES))
def test_sem10_frames_per_job_stay_within_the_fitted_budget(shape: str) -> None:
    """The fit budgets `MAX_NESTED_STARTS` x `FRAMES_PER_NESTED_START`
    frames per job. The frames a cascade needs grow by at most that much
    per job: a change to the oracle that makes a start deeper than the
    budget fails here, before an estate meets it. The ring nests two starts
    per job, the chain one; the fold shape adds a completion of a run begun
    before the cascade to each bypass."""
    small, large = 41, 81
    per_job = (_frames_needed(shape, large) - _frames_needed(shape, small)) / (large - small)
    assert per_job <= MAX_NESTED_STARTS * FRAMES_PER_NESTED_START
    assert per_job > (FRAMES_PER_NESTED_START if shape == "box-ring" else 0)


def test_sem10_equiv_tier_c_fits_the_limit_to_the_larger_catalog() -> None:
    """equiv's tier (c) runs scripts through the oracle, so a script can
    start a cascade as deep as a live one. It fits the limit to the larger
    of the two catalogs."""
    small = lower_source(_box_chain(2)[0])
    large = lower_source(_box_chain(JOBS)[0])
    sys.setrecursionlimit(1000)
    result = equivalent_tier_c(large, large, [[_force_q0()]])
    assert result.equivalent
    assert sys.getrecursionlimit() == fit_recursion_limit(JOBS)
    sys.setrecursionlimit(1000)
    equivalent_tier_c(small, large, [])
    assert sys.getrecursionlimit() == fit_recursion_limit(JOBS)


def test_sem10_a_log_written_past_the_limit_names_the_cascade_and_replays_once_fitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine whose limit fell short dies on the engine-made tick with the
    named error, after the tick is in the WAL. A replay that does not fit the
    limit stops at that input with the same name. A replay that fits it
    replays the log, so a build with the fit recovers such an estate."""
    jil, _ = _box_chain(200)
    (tmp_path / "estate.jil").write_text(jil)
    root = tmp_path / "run"
    monkeypatch.setattr(runner, "fit_recursion_limit", lambda _jobs: sys.getrecursionlimit())
    sys.setrecursionlimit(1000)
    invoke = CliRunner().invoke
    rehearse = ["rehearse", str(tmp_path / "estate.jil"), "--start", START, "--hours", "1"]
    played = invoke(app, [*rehearse, "--run-root", str(root)])
    assert played.exit_code == 1, played.output
    assert "rehearse failed: RecursionError: an instant cascade passed" in played.output
    assert "its first start was q0 (STARTJOB" in played.output
    with monkeypatch.context() as unfitted:
        unfitted.setattr(
            runner_journal, "fit_recursion_limit", lambda _jobs: sys.getrecursionlimit()
        )
        stopped = invoke(app, ["journal", str(root)])
    assert stopped.exit_code == 2, stopped.output
    assert "replay stopped at input 1 (STARTJOB from scheduler" in stopped.output
    assert "CascadeDepthError: RecursionError:" in stopped.output
    assert "its first start was q0" in stopped.output
    replayed = invoke(app, ["journal", str(root)])
    assert replayed.exit_code == 0, replayed.output
    assert "q199 RUNNING->SUCCESS" in replayed.output
