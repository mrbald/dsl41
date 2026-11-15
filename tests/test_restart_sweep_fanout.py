"""S1: a box fan-out with a retry that leaves a TERM-ignoring child, swept (DL-425, DL-426)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from restart_sweep import (
    End,
    BUG88,
    Bug88,
    LINUX,
    Release,
    Scenario,
    Send,
    Step,
    World,
    budget,
    directories,
    gate_command,
    sweep,
    verdict,
)

pytestmark = pytest.mark.usefixtures("cli_env")


def _jil(directory: Path) -> str:
    gate = gate_command(directory)
    return (
        "insert_job: B\njob_type: b\n\n"
        f"insert_job: A\njob_type: c\nbox_name: B\ncommand: {gate}\nn_retrys: 1\n\n"
        f"insert_job: C\njob_type: c\nbox_name: B\ncommand: {gate}\n\n"
        f"insert_job: D\njob_type: c\ncommand: {gate}\ncondition: s(B)\n"
    )


def _rerun_planned(world: World) -> bool:
    return any(p["plan"] == "rerun" for p in world.plans("A"))


FANOUT = Scenario(
    name="fanout",
    jil=_jil,
    # off Linux nothing proves the group once its leader is gone, so no leak ladder holds the
    # retry back (DL-444): the leftover is Linux's part of the scenario
    behaviour={"A": ["1.1"]} if LINUX else {},
    codes=lambda job, _run, ordinal: 1 if (job, ordinal) == ("A", 1) else 0,
    ends={
        # SEM-10: STARTJOB B starts its members. A's exit 1 is a failure (SEM-09) that
        # n_retrys: 1 retries as attempt 2 of the same run (DL-485), which exits 0
        "A": End("SUCCESS", 1, 0),
        "C": End("SUCCESS", 1, 0),  # SEM-09: exit 0 is success
        # SEM-11: SUCCESS once every member ended SUCCESS; a box runs no command, so no code
        "B": End("SUCCESS", 1, None),
        "D": End("SUCCESS", 1, 0),  # SEM-01, SEM-02: s(B) starts D once; exit 0 (SEM-09)
    },
    # every CMD start has a dispatch and a box has none (runner-design §7, run history)
    runs=frozenset({("A", 1), ("C", 1), ("D", 1)}),
    steps=[
        Step("start", lambda _: True, Send("STARTJOB", "B")),
        Step("A fails", lambda w: w.acked("A", 1, 1) and w.acked("C", 1, 1), Release("A", 1, 1)),
        Step("leftover goes", _rerun_planned, Release("A", 1, 1, leftover=True)),
        Step("A retried", lambda w: w.acked("A", 1, 2), Release("A", 1, 2)),
        Step("C ends", lambda w: w.finished("A"), Release("C", 1, 1)),
        Step("D ends", lambda w: w.acked("D", 1, 1), Release("D", 1, 1)),
    ],
)


@pytest.mark.parametrize(
    ("position", "part"),
    [
        pytest.param(
            "before",
            "genesis",
            marks=pytest.mark.xfail(raises=Bug88, strict=True, reason=BUG88),
            id="before-1",
        ),
        pytest.param("before", "rest", id="before-2-on"),
        pytest.param("after", "all", id="after"),
    ],
)
def test_every_crash_point_keeps_the_invariants(position, part):
    """I1 to I10 at every append call of the script's first incarnation (DL-425, DL-426)."""
    with directories() as make:
        reference, points = asyncio.run(sweep(FANOUT, position, make, part))
        assert points, "a sweep with no crash point proves nothing"
        print(budget(reference, points))
        verdict(points, expected=BUG88 if part == "genesis" else None)
