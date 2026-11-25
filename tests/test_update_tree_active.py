"""An update never restarts a box (DL-559). An update that changes box membership is applied
only when nothing has to stop and the touched trees are quiet; otherwise it is refused
`tree_active`, naming its stops and the active jobs of those trees, and it writes nothing.
`reread` forecasts the same refusal from the same function and the same inputs.

The scenarios run on the virtual clock. A command's fake task never ends by itself, so a
started command stays RUNNING and the engine owns its process. Each scenario reads the forecast
(`reread_worker.preview`) and sends the update on the same engine state, and checks the two
agree. The cases are the TODO items the rule closes (BUG-18, BUG-46, BUG-69, BUG-70, BUG-71,
FEAT-31), the edges of "quiet", the stop-set clause, the admission that applies in its own step,
a refusal's retry, and the standalone jobs the rule leaves alone.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from reload_fixtures import prepare_estate
from store_surgery import rows
from test_engine_stops_cookie import PGID, T0
from test_update_atomic import _Ladders
from test_update_owned_processes import KILLJOB, _Box, _killed_by_the_engine, _observed, _teardown
from test_update_settlement import _close, _engine, _estate

from dsl41.ir import CatalogIR
from dsl41.oracle_state import Event
from dsl41.reload_diff import diff
from dsl41.reload_plan import Live, membership_changes, reread, stop_runs, stop_set, tree_active
from dsl41.reload_update import TREE_ACTIVE, Admitted, Refused
from dsl41.reread_worker import preview
from dsl41.runner import Engine
from dsl41.runner_update import UpdateAnswer, UpdateRequest

RESOURCE = "insert_resource: LOCK\nres_type: R\namount: 1\n\n"


def _box(name: str, box: str | None = None, extra: str = "") -> str:
    return (
        f"insert_job: {name}\njob_type: b\n" + (f"box_name: {box}\n" if box else "") + extra + "\n"
    )


def _cmd(name: str, box: str | None = None, extra: str = "", command: str = "sleep 30") -> str:
    return (
        f"insert_job: {name}\njob_type: c\ncommand: {command}\n"
        + (f"box_name: {box}\n" if box else "")
        + extra
        + "\n"
    )


def _fw(name: str, box: str | None = None) -> str:
    return (
        f"insert_job: {name}\njob_type: f\nwatch_file: /synthetic/never\n"
        + (f"box_name: {box}\n" if box else "")
        + "\n"
    )


async def _feed(engine: Engine, *inputs: tuple[str, str] | tuple[str, str, str]) -> None:
    """Feed each (kind, job) or (STATUS, job, status) and let the engine settle after each."""
    for item in inputs:
        payload: dict[str, object] = {"job": item[1]}
        if len(item) == 3:
            payload["status"] = item[2]
        engine.inject(Event(at=T0, kind=item[0], payload=payload))
        await engine.run_until_quiescent(T0)


def _configs(tmp_path: Path) -> list[dict[str, Any]]:
    return [body for _, kind, body in rows(tmp_path / "run") if kind == "config"]


async def _try(
    engine: Engine, tmp_path: Path, text: str, rid: str = "u"
) -> tuple[dict[str, Any], UpdateAnswer, str]:
    """Write `text` as the estate, read the forecast and send the update on one state; answer
    the forecast, the update's answer and the plan's token."""
    _estate(tmp_path, text)
    candidate = prepare_estate(tmp_path / "estate")
    live = engine.update.live()
    forecast = preview(candidate, live)
    token = reread(live, tmp_path / "run" / "plans", candidate=candidate, start=T0).token
    request = UpdateRequest(token, engine.config_rev, engine.baseline_id, rid)
    answer = engine.request_update(request)
    await engine.run_until_quiescent(engine.clock.now())
    return forecast, answer.result(), token


def _refused(
    forecast: dict[str, Any], answer: UpdateAnswer, token: str, names: Sequence[str]
) -> None:
    """The update is refused `tree_active` with `names`, as the forecast says."""
    assert answer.decision == Refused(token, TREE_ACTIVE, tuple(names))
    assert forecast["update_refusal"] == {"code": TREE_ACTIVE, "names": list(names)}


async def _refusal_case(
    tmp_path: Path,
    old: str,
    new: str,
    inputs: Sequence[tuple[str, str] | tuple[str, str, str]],
    names: Sequence[str],
) -> dict[str, tuple[str, int]]:
    """Run `old` with `inputs` fed; the update to `new` is refused with `names` and writes no
    record and no configuration. Answers each row's status and run number afterwards."""
    engine = _engine(tmp_path, old)
    try:
        await _feed(engine, *inputs)
        before = engine.oracle.store.job.copy()
        records = _configs(tmp_path)
        forecast, answer, token = await _try(engine, tmp_path, new)
        _refused(forecast, answer, token, names)
        assert _configs(tmp_path) == records and engine.config_rev == 1
        assert not engine.update.stopping and engine.update.tier is None
        assert {name: (row.status, row.run_number) for name, row in before.items()} == {
            name: (row.status, row.run_number) for name, row in engine.oracle.store.job.items()
        }
        return {name: (row.status, row.run_number) for name, row in engine.oracle.store.job.items()}
    finally:
        await engine.shutdown()
        _close(engine)


# ---------------------------------------------------------------- the pure function


def _catalog(tmp_path: Path, text: str) -> CatalogIR:
    tmp_path.mkdir(parents=True)
    _estate(tmp_path, text)
    return prepare_estate(tmp_path / "estate").catalog


def test_dl559_the_touched_trees_come_from_both_catalogs(tmp_path: Path) -> None:
    """DL-559 rules 1 to 5, read from the function: `M` moves from the tree of `A` into the
    sub-box `B2` of `B`; the standalone `S` is changed and `T` is added outside every box. Only
    `M` changes membership. Both trees are touched, in either catalog: each active job of them
    is named, with the stop set, and a job of no touched tree is not, whatever its row says."""
    old = _catalog(
        tmp_path / "old",
        _box("A") + _cmd("M", "A") + _cmd("A1", "A") + _box("B") + _box("B2", "B") + _cmd("S"),
    )
    new = _catalog(
        tmp_path / "new",
        _box("A")
        + _cmd("A1", "A")
        + _box("B")
        + _box("B2", "B")
        + _cmd("M", "B2")
        + _cmd("S", command="sleep 31")
        + _cmd("T"),
    )
    assert membership_changes(old, new) == frozenset({"M"})
    quiet = {name: "INACTIVE" for name in old.jobs}
    assert tree_active(old, new, quiet, (), ()) == ()
    assert tree_active(old, new, quiet, (), ("S",)) == ("S",)
    status = {**quiet, "A1": "RUNNING", "B2": "QUE_WAIT", "S": "RUNNING"}
    assert tree_active(old, new, status, ("M",), ("S",)) == ("A1", "B2", "M", "S")
    assert tree_active(old, old, status, ("M",), ("S",)) == ()  # no membership change
    # the forecast's stop set adds a named job the engine owns, whatever its row says (DL-525)
    plan = diff(old, new, {}, {})
    inactive = Live(old, {}, 1, {**quiet, "S": "INACTIVE"}, owned={"A1": 1, "S": 3})
    assert stop_set(plan, inactive) == () and stop_runs(plan, inactive) == {"S": 3}
    running = Live(old, {}, 1, {**quiet, "S": "RUNNING"}, runs={"S": 4}, owned={"A1": 1})
    assert stop_runs(plan, running) == {"S": 4}


# ---------------------------------------------------------------- the TODO cases


@pytest.mark.parametrize("case", ["added", "removed", "moved_out", "moved_in"])
def test_feat31_a_member_added_removed_or_moved_under_a_running_box_is_refused(
    tmp_path: Path, case: str
) -> None:
    """DL-559, FEAT-31: under the running box `BX`, a member is added, the running `M2` is
    removed or moved out, or the idle standalone `OUT` moves in. No box is restarted: the
    refusal names the running box and its running members, and nothing changes."""
    old = _box("BX") + _cmd("M1", "BX") + _cmd("M2", "BX") + _cmd("OUT")
    new = {
        "added": old + _cmd("NEW", "BX"),
        "removed": _box("BX") + _cmd("M1", "BX") + _cmd("OUT"),
        "moved_out": _box("BX") + _cmd("M1", "BX") + _cmd("M2") + _cmd("OUT"),
        "moved_in": _box("BX") + _cmd("M1", "BX") + _cmd("M2", "BX") + _cmd("OUT", "BX"),
    }[case]
    rows_after = asyncio.run(
        _refusal_case(tmp_path, old, new, [("FORCE_STARTJOB", "BX")], ["BX", "M1", "M2"])
    )
    assert rows_after["BX"] == ("RUNNING", 1) and rows_after["OUT"] == ("INACTIVE", 0)


def test_bug70_bug46_a_sub_box_moved_out_of_a_running_box_is_refused(tmp_path: Path) -> None:
    """DL-559, BUG-70, BUG-46: the running sub-box `INNER` moves out of the running box `BX`.
    The refusal names the whole running tree, the sub-box's unchanged member included."""
    old = _box("BX") + _box("INNER", "BX") + _cmd("M", "INNER") + _cmd("K", "BX")
    new = _box("BX") + _box("INNER") + _cmd("M", "INNER") + _cmd("K", "BX")
    asyncio.run(
        _refusal_case(tmp_path, old, new, [("FORCE_STARTJOB", "BX")], ["BX", "INNER", "K", "M"])
    )


def test_bug18_bug69_a_running_job_terminator_moved_out_is_refused(tmp_path: Path) -> None:
    """DL-559, BUG-18, BUG-69: the running `job_terminator` member `T` moves out of the running
    box `BX`. The update is refused, so no old box run is ended and nothing ends `T`."""
    terminator = "job_terminator: 1\n"
    old = _box("BX") + _cmd("K", "BX") + _cmd("T", "BX", terminator)
    new = _box("BX") + _cmd("K", "BX") + _cmd("T", None, terminator)
    rows_after = asyncio.run(
        _refusal_case(tmp_path, old, new, [("FORCE_STARTJOB", "BX")], ["BX", "K", "T"])
    )
    assert rows_after["T"] == ("RUNNING", 1)


def test_bug71_a_running_command_retyped_into_a_box_with_members_is_refused(
    tmp_path: Path,
) -> None:
    """DL-559, BUG-71: the running command `BX` becomes a box with the member `NEW`. The
    update is refused by the command, which is also its stop; it is neither stopped nor started
    as a box."""
    rows_after = asyncio.run(
        _refusal_case(
            tmp_path,
            _cmd("BX"),
            _box("BX") + _cmd("NEW", "BX"),
            [("FORCE_STARTJOB", "BX")],
            ["BX"],
        )
    )
    assert rows_after == {"BX": ("RUNNING", 1)}


def test_dl559_a_retyped_member_of_a_running_parent_that_succeeded_is_refused(
    tmp_path: Path,
) -> None:
    """DL-559: the member `M` of the running box `BX` succeeded in its first run and is retyped
    into a box. The update is refused by the running parent and its running member; the parent
    is not stranded and completes when that member ends."""

    async def scenario() -> None:
        old = _box("BX") + _cmd("K", "BX") + _cmd("M", "BX")
        engine = _engine(tmp_path, old)
        try:
            await _feed(engine, ("FORCE_STARTJOB", "BX"), ("STATUS", "M", "SUCCESS"))
            forecast, answer, token = await _try(
                engine, tmp_path, _box("BX") + _cmd("K", "BX") + _box("M", "BX")
            )
            _refused(forecast, answer, token, ["BX", "K"])
            await _feed(engine, ("STATUS", "K", "SUCCESS"))
            assert engine.oracle.store.job["BX"].status == "SUCCESS"
        finally:
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())


def test_dl559_a_running_box_with_no_active_member_removed_is_refused(tmp_path: Path) -> None:
    """DL-559: the box `E` runs while its one member waits on `s(Z)`; the update removes both.
    The refusal names the running box alone: the waiting member is quiet."""
    old = _box("E") + _cmd("M", "E", "condition: s(Z)\n") + _cmd("Z")
    rows_after = asyncio.run(
        _refusal_case(tmp_path, old, _cmd("Z"), [("FORCE_STARTJOB", "E")], ["E"])
    )
    assert rows_after["M"] == ("INACTIVE", 0)


# ---------------------------------------------------------------- what is quiet


def test_dl559_a_queued_job_is_not_quiet(tmp_path: Path) -> None:
    """DL-559 rule 4: `W` waits in QUE_WAIT for the unit `HOLDER` holds; the update moves it
    into the idle box `BX`. A queued row is not quiet: the refusal names `W`, though it has no
    process and is no stop. `HOLDER` is in no touched tree."""
    lock = "resources: (LOCK, QUANTITY=1)\n"
    old = RESOURCE + _cmd("HOLDER", None, lock) + _cmd("W", None, lock) + _box("BX")
    new = RESOURCE + _cmd("HOLDER", None, lock) + _cmd("W", "BX", lock) + _box("BX")
    rows_after = asyncio.run(
        _refusal_case(
            tmp_path, old, new, [("FORCE_STARTJOB", "HOLDER"), ("FORCE_STARTJOB", "W")], ["W"]
        )
    )
    assert rows_after["W"] == ("QUE_WAIT", 0)


def test_dl559_an_inactive_row_whose_process_runs_is_not_quiet(tmp_path: Path) -> None:
    """DL-559 rule 4, DL-235: an operator sets the running member `j` INACTIVE; its command
    runs on and its box `BX` completes. The engine owns `j`'s process, so `j` is not quiet and
    the update that adds a member to `BX` is refused by `j` alone."""
    old = _box("BX") + _cmd("j", "BX")
    rows_after = asyncio.run(
        _refusal_case(
            tmp_path,
            old,
            old + _cmd("NEW", "BX"),
            [("FORCE_STARTJOB", "BX"), ("STATUS", "j", "INACTIVE")],
            ["j"],
        )
    )
    assert rows_after == {"BX": ("SUCCESS", 1), "j": ("INACTIVE", 1)}


def test_dl559_the_worker_snapshot_carries_the_names_the_engine_owns(tmp_path: Path) -> None:
    """DL-559 rule 7: `reread`'s worker reads the engine's snapshot as the control server sends
    it, through JSON. It carries the names the engine owns a process of, so the worker forecasts
    the refusal by the INACTIVE `j` whose command runs, as the admission refuses it."""
    import json
    from dataclasses import asdict

    from dsl41.reload_diff import catalog_payload
    from dsl41.reread_worker import run

    async def scenario() -> None:
        old = _box("BX") + _cmd("j", "BX")
        engine = _engine(tmp_path, old)
        try:
            await _feed(engine, ("FORCE_STARTJOB", "BX"), ("STATUS", "j", "INACTIVE"))
            live = engine.update.live()
            assert live.owned == {"j": 1}
            _estate(tmp_path, old + _cmd("NEW", "BX"))
            payload = {**asdict(live), "catalog": catalog_payload(live.catalog)}
            request = {
                "source": str(tmp_path / "estate" / "dsl41.toml"),
                "run_root": str(tmp_path / "run"),
                "update": False,
                "live": json.loads(json.dumps(payload)),
            }
            forecast = run(request)["preview"]
            _, answer, token = await _try(engine, tmp_path, old + _cmd("NEW", "BX"))
            _refused(forecast, answer, token, ["j"])
        finally:
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())


def test_dl559_a_terminal_row_whose_stop_still_runs_is_not_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-559 rule 4, DL-525: a KILLJOB makes the member `j` TERMINATED at once while the
    engine's stop of its command still runs, and its box `BX` ends. The engine still owns that
    process, so the update that adds a member to `BX` is refused by `j`; once the stop is done,
    the same update is admitted and applies at once."""
    procs = _Box(monkeypatch)
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID)
    old = _box("BX") + _cmd("j", "BX")
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, old)
        try:
            await _feed(engine, ("FORCE_STARTJOB", "BX"))
            attempt, wrapper = _observed(engine, "j", PGID)
            procs.run_id = attempt
            await _killed_by_the_engine(engine, ladders, attempt, wrapper, KILLJOB)
            seen.append({name: row.status for name, row in engine.oracle.store.job.items()})
            forecast, answer, token = await _try(engine, tmp_path, old + _cmd("NEW", "BX"))
            _refused(forecast, answer, token, ["j"])
            await ladders.end(engine, attempt, PGID)
            await engine.run_until_quiescent(T0)
            assert engine.owned_runs() == {}
            forecast, answer, _ = await _try(engine, tmp_path, old + _cmd("NEW", "BX"), "again")
            assert forecast["update_refusal"] is None
            assert isinstance(answer.decision, Admitted) and engine.config_rev == 2
        finally:
            _teardown(engine, ladders)
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    assert seen == [{"BX": "FAILURE", "j": "TERMINATED"}]


@pytest.mark.parametrize("running", [False, True])
def test_dl559_held_or_iced_jobs_with_quiet_rows_do_not_refuse(
    tmp_path: Path, running: bool
) -> None:
    """DL-559 rule 4: in the box `BX`, `M` is on hold and `H` on ice. With `BX` idle, the
    update that adds a member is admitted and applies at once. With `BX` started, it is refused
    by the running boxes only: the held and iced members are quiet."""
    old = _box("BX") + _box("INNER", "BX") + _cmd("M", "INNER") + _cmd("H", "BX")
    new = old + _cmd("NEW", "BX")
    inputs: list[tuple[str, str]] = [("ON_HOLD", "M"), ("ON_ICE", "H")]
    if running:
        asyncio.run(
            _refusal_case(tmp_path, old, new, [*inputs, ("FORCE_STARTJOB", "BX")], ["BX", "INNER"])
        )
        return

    async def scenario() -> None:
        engine = _engine(tmp_path, old)
        try:
            await _feed(engine, *inputs)
            forecast, answer, _ = await _try(engine, tmp_path, new)
            assert forecast["update_refusal"] is None
            assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == ()
            assert engine.config_rev == 2 and "NEW" in engine.oracle.catalog.jobs
            row = engine.oracle.store.job["M"]
            assert (row.on_hold, engine.oracle.store.job["H"].on_ice) == (True, True)
        finally:
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())


@pytest.mark.parametrize("how", ["forced", "parent_killed"])
def test_dl559_a_sub_box_open_under_a_top_level_box_that_is_not_running_refuses(
    tmp_path: Path, how: str
) -> None:
    """DL-559 rule 3: the sub-box `INNER` runs while the top-level box `BX` does not: it was
    forced on its own, or `BX` was killed while its members run on. The update that adds a
    member to `BX` touches the whole tree, so the running sub-box and members refuse it."""
    old = _box("BX") + _box("INNER", "BX") + _cmd("M", "INNER") + _cmd("H", "BX")
    inputs = (
        [("FORCE_STARTJOB", "INNER")]
        if how == "forced"
        else [("FORCE_STARTJOB", "BX"), ("KILLJOB", "BX")]
    )
    names = ["INNER", "M"] if how == "forced" else ["H", "INNER", "M"]
    rows_after = asyncio.run(_refusal_case(tmp_path, old, old + _cmd("NEW", "BX"), inputs, names))
    assert rows_after["BX"] == (("INACTIVE", 0) if how == "forced" else ("TERMINATED", 1))


def test_dl559_a_running_job_of_an_untouched_tree_does_not_refuse(tmp_path: Path) -> None:
    """DL-559 rule 3: the box `OTHER` and the standalone `RUNNER` run, unchanged; the update
    adds a member to the idle box `BX`. It is admitted and applied, and the running jobs keep
    their runs."""

    async def scenario() -> None:
        old = _box("BX") + _cmd("M", "BX") + _box("OTHER") + _cmd("O", "OTHER") + _cmd("RUNNER")
        engine = _engine(tmp_path, old)
        try:
            await _feed(engine, ("FORCE_STARTJOB", "OTHER"), ("FORCE_STARTJOB", "RUNNER"))
            forecast, answer, _ = await _try(engine, tmp_path, old + _cmd("NEW", "BX"))
            assert forecast["update_refusal"] is None
            assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == ()
            assert engine.config_rev == 2
            store = engine.oracle.store.job
            assert {name: (store[name].status, store[name].run_number) for name in store} == {
                "BX": ("INACTIVE", 0),
                "M": ("INACTIVE", 0),
                "NEW": ("INACTIVE", 0),
                "OTHER": ("RUNNING", 1),
                "O": ("RUNNING", 1),
                "RUNNER": ("RUNNING", 1),
            }
        finally:
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())


# ---------------------------------------------------------------- the stop-set clause


def test_dl559_a_stop_elsewhere_refuses_a_membership_change_in_a_quiet_tree(
    tmp_path: Path,
) -> None:
    """DL-559 rule 5: the update adds a member to the idle box `BX` and changes the running
    standalone `CHG`. The tree is quiet, but `CHG` would have to stop: the refusal names it,
    and the forecast lists it as its one stop. Without the membership change the same edit is
    admitted and stops `CHG`, as before (DL-424)."""
    old = _box("BX") + _cmd("M", "BX") + _cmd("CHG")
    edited = old.replace(
        "insert_job: CHG\njob_type: c\ncommand: sleep 30",
        "insert_job: CHG\njob_type: c\ncommand: sleep 31",
    )
    with_member = edited + _cmd("NEW", "BX")

    async def scenario() -> None:
        engine = _engine(tmp_path, old)
        try:
            await _feed(engine, ("FORCE_STARTJOB", "CHG"))
            forecast, answer, token = await _try(engine, tmp_path, with_member)
            _refused(forecast, answer, token, ["CHG"])
            assert forecast["stop"] == ["CHG"]
            assert forecast["effects"] == [{"job": "CHG", "action": "rerun"}]
            forecast, answer, _ = await _try(engine, tmp_path, edited, "plain")
            assert forecast["update_refusal"] is None
            assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == ("CHG",)
        finally:
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())


# ---------------------------------------------------------------- admitted: one step, no roots


def test_dl559_a_membership_change_in_quiet_trees_applies_in_its_admission_step(
    tmp_path: Path,
) -> None:
    """DL-559 rule 6: the update adds `NEW` to the idle box `BX` and removes its member `OLD`.
    It is admitted with nothing to stop and no root: the intent records `restart_boxes: []`, and
    the application follows it in the same step, with no input between. A start of `BX` queued
    right behind the request opens its run under the new members: `NEW` starts, `OLD` is gone."""

    async def scenario() -> None:
        old = _box("BX") + _cmd("KEEP", "BX") + _cmd("OLD", "BX")
        engine = _engine(tmp_path, old)
        try:
            _estate(tmp_path, _box("BX") + _cmd("KEEP", "BX") + _cmd("NEW", "BX"))
            candidate = prepare_estate(tmp_path / "estate")
            live = engine.update.live()
            token = reread(live, tmp_path / "run" / "plans", candidate=candidate, start=T0).token
            answer = engine.request_update(UpdateRequest(token, 1, engine.baseline_id, "one-step"))
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "BX"}))
            await engine.run_until_quiescent(T0)
            decision = answer.result().decision
            assert isinstance(decision, Admitted) and decision.stop_set == ()
            assert engine.config_rev == 2
            store = engine.oracle.store
            assert "OLD" not in store.job and store.tombstones["OLD"].run_number == 0
            assert {name: store.job[name].status for name in ("BX", "KEEP", "NEW")} == {
                "BX": "RUNNING",
                "KEEP": "RUNNING",
                "NEW": "RUNNING",
            }
        finally:
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    journal = [(kind, body) for _, kind, body in rows(tmp_path / "run")]
    [intent] = [
        i for i, (k, b) in enumerate(journal) if (k, b.get("event")) == ("config", "intent")
    ]
    [applied] = [
        i
        for i, (k, b) in enumerate(journal)
        if (k, b.get("event")) == ("config", "applied") and "token" in b
    ]
    assert journal[intent][1]["restart_boxes"] == [] and journal[intent][1]["stop"] == []
    assert applied == intent + 1
    [start] = [
        i
        for i, (k, b) in enumerate(journal)
        if k == "input" and b.get("kind") == "FORCE_STARTJOB" and b["payload"]["job"] == "BX"
    ]
    assert start > applied
    dispatched = [b["job"] for k, b in journal if k == "dispatch"]
    assert sorted(dispatched) == ["KEEP", "NEW"]


# ---------------------------------------------------------------- a refusal and its retry


def test_dl559_a_refusal_writes_nothing_and_its_retry_is_decided_on_the_state_then(
    tmp_path: Path,
) -> None:
    """DL-559 rule 5: a refusal writes no record and no configuration revision, so its request
    id is not taken. A retry under the same id is refused again while the tree runs, and
    admitted once it is quiet."""

    async def scenario() -> None:
        old = _box("BX") + _cmd("M", "BX")
        new = old + _cmd("NEW", "BX")
        engine = _engine(tmp_path, old)
        try:
            await _feed(engine, ("FORCE_STARTJOB", "BX"))
            records = _configs(tmp_path)
            for _ in range(2):
                forecast, answer, token = await _try(engine, tmp_path, new, "same")
                _refused(forecast, answer, token, ["BX", "M"])
                assert _configs(tmp_path) == records
                assert engine.journal is not None
                assert engine.journal.store.find_request("same") is None
            await _feed(engine, ("STATUS", "M", "SUCCESS"))
            assert engine.oracle.store.job["BX"].status == "SUCCESS"
            forecast, answer, _ = await _try(engine, tmp_path, new, "same")
            assert forecast["update_refusal"] is None
            assert isinstance(answer.decision, Admitted) and engine.config_rev == 2
        finally:
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())


# ---------------------------------------------------------------- what the rule leaves alone


@pytest.mark.parametrize("case", ["added", "removed", "retyped"])
def test_dl559_a_standalone_job_added_removed_or_retyped_while_it_runs_is_as_before(
    tmp_path: Path, case: str
) -> None:
    """DL-559 rule 1: a job outside every box in both catalogs that is no BOX in either changes
    no membership. Added beside the running `R`, or `R` removed or retyped into a file watch
    while it runs: the update is admitted, stops `R` unless it is only added beside it, and
    applies, as before (DL-424)."""
    old = _cmd("R")
    new = {"added": old + _cmd("NEW"), "removed": _cmd("OTHER"), "retyped": _fw("R")}[case]

    async def scenario() -> None:
        engine = _engine(tmp_path, old)
        try:
            await _feed(engine, ("FORCE_STARTJOB", "R"))
            forecast, answer, _ = await _try(engine, tmp_path, new)
            assert forecast["update_refusal"] is None
            decision = answer.decision
            assert isinstance(decision, Admitted)
            assert decision.stop_set == (() if case == "added" else ("R",))
            assert engine.config_rev == 2
            catalog = engine.oracle.catalog.jobs
            assert ("R" in catalog) == (case != "removed")
            if case == "retyped":
                assert catalog["R"].job_type == "FW"
        finally:
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    [intent] = [body for body in _configs(tmp_path) if body["event"] == "intent"]
    assert intent["restart_boxes"] == []


# ---------------------------------------------------------------- the order of refusals


def test_dl559_a_preparation_refusal_comes_before_the_forecast_tree_active(
    tmp_path: Path,
) -> None:
    """DL-559 rule 7: the forecast follows the order the update refuses in. With the engine's
    host launch settings changed, the update's preparation refuses first (`invalid_argument`),
    so the forecast gives no `tree_active` for a member added under the running box; with
    them unchanged it does."""
    from dataclasses import replace

    from dsl41.reload_plan import RereadRefused

    async def scenario() -> None:
        old = _box("BX") + _cmd("M", "BX")
        engine = _engine(tmp_path, old)
        try:
            await _feed(engine, ("FORCE_STARTJOB", "BX"))
            _estate(tmp_path, old + _cmd("NEW", "BX"))
            candidate = prepare_estate(tmp_path / "estate")
            live = engine.update.live()
            forecast = preview(candidate, live)
            assert forecast["update_refusal"] == {"code": TREE_ACTIVE, "names": ["BX", "M"]}
            moved = replace(live, launch={**live.launch, "machine_policy": "synthetic-other"})
            forecast = preview(candidate, moved)
            assert forecast["update_refusal"] is None
            [blocked] = forecast["definition_changes"]["blocked"]
            assert blocked.startswith("engine host launch settings changed: machine_policy")
            with pytest.raises(RereadRefused, match="engine host launch settings changed"):
                reread(moved, tmp_path / "run" / "plans", candidate=candidate, start=T0)
        finally:
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())


# ---------------------------------------------------------------- one rule for owned stops


@pytest.mark.usefixtures("cli_env")
def test_dl559_an_edited_job_whose_process_runs_under_an_inactive_row_refuses_over_the_socket(
    short_root: Path,
) -> None:
    """DL-559 rules 5 and 7, DL-525, DL-235: over the socket, the standalone `J` runs, and an
    operator sets its row INACTIVE while its command runs on. One update edits `J` and adds a
    member to the idle box `BX`. Its stops come from one rule (`reload_plan.stop_runs`): the
    process the engine owns makes `J` a stop though its row is not live, so `reread` forecasts
    `tree_active` naming `J`, and `update` is refused with the same name and writes nothing.
    The forecast's stop list stays the rows' alone, which names no job."""
    from test_main_config import JOB, config_file
    from test_main_config_restart import cli_engine
    from test_runner_control import _sync_sendevent, wait_for

    from dsl41.runner_control import roundtrip

    path = config_file(short_root)
    member = JOB.replace("A", "M") + "box_name: BX\n"
    command = JOB.replace("A", "J").replace("true", "sleep 30")
    old = "insert_job: BX\njob_type: b\n" + member + command
    (short_root / "a.jil").write_text(old)
    root = short_root / "run"
    root.mkdir()
    (root / "engine.toml").write_text("grace_term_s = 0.01\ngrace_kill_s = 0.01\n")
    socket = root / "control.sock"
    with cli_engine(path) as ask:
        assert _sync_sendevent(socket, "FORCE_STARTJOB", job="J")["ok"]
        wait_for(lambda: ask("status")["jobs"]["J"]["status"] == "RUNNING")
        assert _sync_sendevent(socket, "CHANGE_STATUS", job="J", status="INACTIVE")["ok"]
        wait_for(lambda: ask("status")["jobs"]["J"]["status"] == "INACTIVE")
        (short_root / "a.jil").write_text(
            "insert_job: BX\njob_type: b\n"
            + member
            + JOB.replace("A", "NEW")
            + "box_name: BX\n"
            + command.replace("sleep 30", "sleep 31")
        )
        view = roundtrip(socket, {"v": 5, "cmd": "reread"})["preview"]
        assert view["stop"] == []
        assert view["update_refusal"] == {"code": TREE_ACTIVE, "names": ["J"]}
        refused = roundtrip(
            socket,
            {
                "v": 5,
                "cmd": "update",
                "wait": True,
                "request_id": "owned",
                "baseline_id": ask("status")["baseline_id"],
            },
        )
        assert not refused["ok"] and (refused["code"], refused["names"]) == (TREE_ACTIVE, ["J"])
        assert ask()["revision"] == 1
        assert not [
            body
            for _, kind, body in rows(root)
            if kind == "config" and body.get("event") == "intent"
        ]


@pytest.mark.parametrize("case", ["stopping", "draining"])
def test_dl559_an_open_update_or_a_drain_comes_before_the_forecast_tree_active(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """DL-559 rule 7: over the socket, `reread` forecasts the refusal a fresh update on the same
    state gets. A member is added under the running box `BX`. While another update waits for a
    stop (`stopping`), or while the engine drains (`draining`), the update is refused for that
    first, so the answer forecasts no `tree_active`; it still shows the whole diff and says why
    the update would refuse. The stop that never ends, and the drain that never ends, are
    stand-ins that hold those windows open; a real drain over a real engine process is in
    `test_live_engine_branches` (Linux only)."""
    from datetime import datetime

    from test_main_config import JOB, config_file

    from dsl41 import main_config as main
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import RealClock
    from dsl41.runner_control import ControlServer, roundtrip
    from dsl41.runner_startup import start_run
    from dsl41.reread_worker import preview

    path = config_file(short_root)
    box = "insert_job: BX\njob_type: b\n" + JOB.replace("A", "M") + "box_name: BX\n"
    job = JOB.replace("A", "J")
    (short_root / "a.jil").write_text(box + job)
    initial = main.prepare(path)

    async def scenario() -> None:
        engine = start_run(
            initial.catalog,
            short_root / "run",
            clock=RealClock(),
            adapters={"CMD": FakeAdapter(default=None)},
            hold_open=True,
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        engine.reread_source = str(path)
        engine.launch_settings = initial.metadata["launch"]
        server = ControlServer(engine, short_root / "control.sock")
        await server.start()
        loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))

        async def ask(cmd: str, **fields: Any) -> dict[str, Any]:
            request = {"v": 5, "cmd": cmd, **fields}
            return await asyncio.to_thread(roundtrip, server.path, request, timeout=30)

        def start(name: str) -> dict[str, Any]:
            return {"baseline_id": engine.baseline_id, "request_id": name}

        try:
            await engine.started.wait()
            for name in ("BX", "J"):
                status = await ask("status")
                reply = await ask(
                    "sendevent",
                    verb="FORCE_STARTJOB",
                    payload={"job": name},
                    expect={f"job:{name}": status["jobs"][name]["state_rev"]},
                    **start(f"start-{name}"),
                )
                assert reply["ok"], reply
            if case == "stopping":
                # the update's stop of J never ends, so the update stays STOPPING
                monkeypatch.setattr(engine.update, "stop_attempt", lambda job, cause: None)
                (short_root / "a.jil").write_text(box + job.replace("true", "echo j"))
                first = await ask("update", wait=False, **start("first"))
                assert first["ok"] and first["stop"] == ["J"], first
                assert engine.update.stopping
                text = box + job.replace("true", "echo j")
            else:
                # the engine's drain is held open for the reread and the update
                monkeypatch.setattr(type(engine), "draining", property(lambda _self: True))
                text = box + job
            (short_root / "a.jil").write_text(text + JOB.replace("A", "NEW") + "box_name: BX\n")
            # the pure forecast alone says `tree_active`; the server's answer must not
            own = preview(main.prepare(path), engine.update.live())["update_refusal"]
            names = ["BX", "J", "M"] if case == "stopping" else ["BX", "M"]
            assert own == {"code": "tree_active", "names": names}
            answer = await ask("reread")
            assert answer["ok"], answer
            view = answer["preview"]
            assert view["added"] == ["NEW"] and view["update_refusal"] is None
            note = (
                "another update is open or preparing; wait for it to finish"
                if case == "stopping"
                else "the engine is draining; it takes no update until it restarts"
            )
            assert note in view["definition_changes"]["blocked"]
            fresh = await ask("update", wait=False, **start("fresh"))
            assert not fresh["ok"], fresh
            code = "update_in_progress" if case == "stopping" else "engine_draining"
            assert fresh["code"] == code
        finally:
            await server.close()
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())
