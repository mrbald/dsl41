"""S2: a retry plan superseded while an overdue timer owes a start at the restart, swept
(BUG-86, DL-425, DL-426)."""

from __future__ import annotations

from pathlib import Path

import pytest
from restart_sweep import (
    POSITIONS,
    check,
    End,
    BEGINNING,
    LINUX,
    Release,
    Restart,
    Scenario,
    Send,
    Step,
    World,
    seen,
    gate_command,
)

pytestmark = pytest.mark.usefixtures("cli_env")


def _jil(directory: Path) -> str:
    gate = gate_command(directory)
    return (
        f"insert_job: J\njob_type: c\ncommand: {gate}\ncondition: t(K)\nn_retrys: 1\n\n"
        f"insert_job: K\njob_type: c\ncommand: {gate}\nterm_run_time: 1\n"
    )


def _rerun_planned(world: World) -> bool:
    return any(p["plan"] == "rerun" for p in world.plans("J"))


def _code(job: str, run: int, _ordinal: int) -> int | None:
    if job == "K":
        return None  # K's term_run_time ends it
    return 1 if run == 1 else 0


OVERDUE = Scenario(
    name="overdue",
    jil=_jil,
    # J.1 leaves a TERM-ignoring child, so its retry plan waits for the leak ladder across the
    # restart; off Linux nothing proves the group once its leader is gone (DL-444), so the plan
    # dispatches at once and the leftover is Linux's part of the scenario
    behaviour={"J": ["1.*"]} if LINUX else {},
    codes=_code,
    ends={
        # FORCE_STARTJOB starts run 1, which exits 1 (SEM-09) and is retried (DL-485). On Linux
        # the retry waits for the leak ladder of J.1's leftover, CHANGE_STATUS INACTIVE moves
        # the row past the run first, and the restart never launches the retry: a start the
        # current row superseded is not launched (runner-design §7). Off Linux no ladder holds
        # it (DL-444): the retry is dispatched at once, is lost at the restart, and its plan
        # is `none`, since its run is no longer the row's live run (the interruption rule).
        # Either way K's TERMINATED makes t(K) true (SEM-02), which starts run 2 (SEM-01); it
        # exits 0 (SEM-09)
        "J": End("SUCCESS", 2, 0),
        # term_run_time: 1 ends run 1 TERMINATED (autosys-semantics, attribute table); the
        # command never exits by itself, so no exit code reaches the row
        "K": End("TERMINATED", 1, None),
    },
    # every CMD start has a dispatch (runner-design §7, run history)
    runs=frozenset({("J", 1), ("K", 1), ("J", 2)}),
    # the clock stands at BEGINNING until K's STARTJOB is in the store, then past K's deadline
    clock=(seen("input", kind="STARTJOB", **{"payload.job": "K"}),),
    # the first incarnation never moves its loop past BEGINNING: K's timer is overdue only at
    # the restart (DL-491)
    horizon=BEGINNING,
    steps=[
        Step("J forced", lambda _: True, Send("FORCE_STARTJOB", "J")),
        Step("J fails", lambda w: w.acked("J", 1, 1), Release("J", 1, 1)),
        Step("J inactive", _rerun_planned, Send("CHANGE_STATUS", "J", {"status": "INACTIVE"})),
        Step("K starts", lambda _: True, Send("STARTJOB", "K")),
        # K.1 runs; after the fault its deadline may end it before it is launched (DL-425)
        Step("restart", lambda w: w.started("K", 1) or w.status("K") == "TERMINATED", Restart()),
        Step("J.2 ends", lambda w: w.after_fault or w.acked("J", 2, 1), Release("J", 2, 1)),
    ],
)


@pytest.mark.parametrize(("position", "part"), POSITIONS)
def test_every_crash_point_keeps_the_invariants(position, part):
    """I1 to I10 at every append call of every engine process of the history."""
    check(OVERDUE, position, part)
