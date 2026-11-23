"""While an update waits, no process is launched for a job it names, also when the plan leaves
the job unchanged (DL-522, DL-525, DL-527). The named jobs are the plan's added, changed and
removed jobs, the admitted box roots, and their descendants in the old and in the new catalog. A
start the oracle decides for one waits as `dispatch_pending`. One whose run ends while it waits
is discharged with the input that ends it (DL-551). After the application the rest are released:
discharged with no launch when the run is no longer wanted, else launched under the applied
definition. Any other job starts at once, and the box restart's cascade can still end one
(DL-527, THE LIMIT).

The scenarios run on the virtual clock with `test_engine_stops_cookie`'s process stand-ins. The
update waits for `c`, a running command of the restarting box `R` whose stop sequence the test
parks and ends (`test_update_atomic._Ladders`). Each `Engine._launch` is counted.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from reload_fixtures import prepare_estate, write_config
from store_surgery import drop_snapshots, rows
from store_surgery import update as rewrite
from test_engine_stops_cookie import PGID, T0, _Procs
from test_update_atomic import _Ladders, _update_steps
from test_update_owned_processes import _crash, _observed, _started, _teardown
from test_update_settlement import SIGNALED, _close, _plan, _request
from test_update_watches import _launches

from dsl41 import main_config as main
from dsl41.oracle_state import Event
from dsl41.reload_update import Admitted
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter, FileWatcherAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_startup import resume_run, start_run

SETTINGS = EngineSettings(grace_term_s=0.05, grace_kill_s=0.1)


def _job(name: str, body: str, box: str | None = None) -> str:
    return f"insert_job: {name}\n{body}" + (f"box_name: {box}\n" if box else "") + "\n"


def _cmd(condition: str | None = None, extra: str = "") -> str:
    return (
        "job_type: c\ncommand: sleep 30\n"
        + (f"condition: {condition}\n" if condition else "")
        + extra
    )


def _fw(tmp_path: Path, condition: str, extra: str = "") -> str:
    return (
        f"job_type: f\nwatch_file: {tmp_path / 'never'}\nwatch_interval: 60\n"
        f"condition: {condition}\n{extra}"
    )


def _set(name: str) -> Event:
    return Event(at=T0, kind="SET_GLOBAL", payload={"name": name, "value": "go"})


def _force(job: str) -> Event:
    return Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job})


def _estate(tmp_path: Path, text: str, marked: tuple[str, ...]) -> None:
    directory = tmp_path / "estate"
    write_config(directory)
    (directory / "jobs.jil").write_text(text)
    lines = "".join(f'"{job}" = "it must not run twice"\n' for job in marked)
    (directory / "non-idempotent.toml").write_text(f"[jobs]\n{lines}" if lines else "")


def _adapters() -> dict[str, Any]:
    return {"CMD": FakeAdapter(default=None), "FW": FileWatcherAdapter(existence="stable")}  # type: ignore[arg-type]


def _start(tmp_path: Path, text: str, marked: tuple[str, ...]) -> Engine:
    _estate(tmp_path, text, marked)
    estate = prepare_estate(tmp_path / "estate")
    return start_run(
        estate.catalog,
        tmp_path / "run",
        source_capture=main.encoded_parts(estate),
        launched=main.profile_of(estate),
        clock=VirtualClock(start=T0),
        adapters=_adapters(),
        settings=SETTINGS,
    )


def _journal(tmp_path: Path) -> list[tuple[int, str, dict[str, Any]]]:
    return rows(tmp_path / "run")


def _applied_seq(journal: list[tuple[int, str, dict[str, Any]]]) -> int | None:
    found = [
        seq
        for seq, kind, body in journal
        if (kind, body.get("event")) == ("config", "applied") and "token" in body
    ]
    return found[0] if found else None


def _starts(tmp_path: Path, job: str) -> list[tuple[str, int | None, bool]]:
    """`job`'s start records in order: each `dispatch_pending` (`pending` with its run, then
    `discharged` with no dispatch) and each `dispatch` with its run (one that carries a pending
    start discharges it), each with whether it follows the update's `config applied`."""
    journal = _journal(tmp_path)
    applied = _applied_seq(journal)
    pending = {
        seq
        for seq, kind, body in journal
        if kind == "dispatch_pending" and body.get("event") == "pending" and body["job"] == job
    }
    out: list[tuple[str, int | None, bool]] = []
    for seq, kind, body in journal:
        after = applied is not None and seq > applied
        if kind == "dispatch_pending" and body.get("event") == "pending" and body["job"] == job:
            out.append(("pending", body["run_number"], after))
        elif (
            kind == "dispatch_pending"
            and body.get("event") == "discharged"
            and body.get("pending") in pending
            and body.get("dispatch") is None
        ):
            out.append(("discharged", None, after))
        elif kind == "dispatch" and body.get("job") == job:
            out.append(("dispatch", body["run_number"], after))
    return out


def _dispatch_revs(tmp_path: Path, job: str) -> list[tuple[int, int]]:
    """Each `dispatch` of `job`: its run number and the configuration rev it ran under."""
    return [
        (body["run_number"], body["rev"])
        for _seq, kind, body in _journal(tmp_path)
        if kind == "dispatch" and body.get("job") == job
    ]


def _row(engine: Engine, job: str) -> tuple[str, int, bool]:
    row = engine.oracle.store.job[job]
    return row.status, row.run_number, row.on_hold


async def _admit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launches: list[tuple[str, int, str | None, Any]],
    old: str,
    new: str,
    *,
    before: tuple[Event, ...],
    during: tuple[Event, ...],
    watch: tuple[str, ...],
    marked: tuple[str, ...] = (),
    crash: str | None = None,
    advance: bool = False,
) -> dict[str, Any]:
    """Run `old` with `before` fed; the update to `new` waits for `c`'s stop; `during` is fed,
    and with `advance` the clock then moves to the oracle's next timer while the update waits.
    Answers the admission, then each job of `watch`'s launches and row while the update waits.
    With `crash` the engine then dies (`snapshot`: with a snapshot taken after the admission,
    before `during`); else `c`'s stop ends, and each job's launches and row after the application."""
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID)
    engine = _start(tmp_path, old, marked)
    seen: dict[str, Any] = {}
    stopped = False
    try:
        for event in before:
            engine.inject(event)
        await engine.run_until_quiescent(T0)
        attempt, wrapper = _observed(engine, "c", PGID)
        assert wrapper is not None
        seen["c"] = attempt
        seen["before"] = {job: _row(engine, job) for job in watch}
        _estate(tmp_path, new, marked)
        answer = await _request(engine, _plan(engine, tmp_path))
        assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == ("c",)
        seen["filter"] = answer.decision.dispatch_filter
        await _started(ladders, PGID)
        if crash == "snapshot":
            engine.take_snapshot()
        del launches[:]
        for event in during:
            engine.inject(event)
        await engine.run_until_quiescent(T0)
        if advance:
            due = engine.oracle.next_timer_due()
            assert due is not None and due > T0
            await engine.clock.wait_until(due)
            # the loop takes a timer due before its horizon
            await engine.run_until_quiescent(due + timedelta(seconds=1))
        assert engine.update.stopping and engine.config_rev == 1
        seen["during"] = {
            job: ([launch[1] for launch in launches if launch[0] == job], _row(engine, job))
            for job in watch
        }
        if crash is not None:
            stopped = True
            await _crash(engine)
            return seen
        engine.attempts.status_recorded(attempt, SIGNALED)
        wrapper.cancel()
        await engine.run_until_quiescent(engine.clock.now())
        assert engine.update.stopping
        async with asyncio.timeout(30):  # a guard against a hang only
            await ladders.end(engine, attempt, PGID)
        await engine.run_until_quiescent(engine.clock.now())
        assert not engine.update.stopping and engine.config_rev == 2
        seen["after"] = {
            job: ([launch[1] for launch in launches if launch[0] == job], _row(engine, job))
            for job in watch
        }
    finally:
        if not stopped:
            _teardown(engine, ladders)
            await engine.shutdown()
            _close(engine)
    return seen


async def _restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launches: list[tuple[str, int, str | None, Any]],
    attempt: str,
    watch: tuple[str, ...],
) -> dict[str, Any]:
    """Restart two minutes on; at the loop's first look record the filter and each job of
    `watch`'s launches and row, then end `c`'s stop and record them after the application."""
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID)
    _steps, first = _update_steps(monkeypatch)
    at = T0 + timedelta(minutes=2)
    engine = await resume_run(
        prepare_estate(tmp_path / "estate").catalog,
        tmp_path / "run",
        clock=VirtualClock(start=at),
        adapters=_adapters(),
        settings=SETTINGS,
    )
    seen: dict[str, Any] = {}
    del launches[:]
    loop = asyncio.ensure_future(engine.run_until_quiescent(at))
    try:
        async with asyncio.timeout(30):  # a guard against a hang only
            await first.wait()
            assert engine.update.tier is not None and engine.update.stopping
            seen["filter"] = engine.update.tier.dispatch_filter
            seen["during"] = {
                job: ([launch[1] for launch in launches if launch[0] == job], _row(engine, job))
                for job in watch
            }
            await ladders.running(PGID)
            await ladders.end(engine, attempt, PGID)
            await loop
        await engine.run_until_quiescent(at)
        assert not engine.update.stopping and engine.config_rev == 2
        seen["after"] = {
            job: ([launch[1] for launch in launches if launch[0] == job], _row(engine, job))
            for job in watch
        }
    finally:
        _teardown(engine, ladders)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        await engine.shutdown()
        _close(engine)
    return seen


# ---------------------------------------------------------------- M1: a nested member is kept back


def _nested(tmp_path: Path, kind: str, wake: str, *, added: bool) -> str:
    """`R` holds `c` and the sub-box `B`; `B` holds `m`, a command or a watch woken by `wake`,
    and the marked command `m2`. With `added`, `R` also holds `n`, so the update restarts `R`.
    For `capacity`, the unrelated `h` holds the one unit of `SLOT` that `m` needs."""
    capacity = wake == "capacity"
    extra = "resources: (SLOT, QUANTITY=1, FREE=A)\n" if capacity else ""
    member = (
        _cmd('v(GO) = "go"', extra) if kind == "command" else _fw(tmp_path, 'v(GO) = "go"', extra)
    )
    text = (
        _job("R", "job_type: b\n")
        + _job("c", _cmd(), "R")
        + _job("B", "job_type: b\n", "R")
        + _job("m", member, "B")
        + _job("m2", _cmd('v(GO2) = "go"'), "B")
    )
    if capacity:
        text = (
            "insert_resource: SLOT\nres_type: R\namount: 1\n\n"
            + _job("h", _cmd(extra=extra))
            + text
        )
    if added:
        text += _job("n", _cmd(), "R")
    return text


def _wakes(wake: str) -> tuple[tuple[Event, ...], tuple[Event, ...]]:
    """What is fed before the update and while it waits: `m` is woken by its condition, by an
    operator's FORCE_STARTJOB, or by the release of the unit it queues for; `GO` is set by then
    in each case, so `m` is eligible in the new box run. `m2` is woken by its condition."""
    if wake == "condition":
        return (_force("R"),), (_set("GO"), _set("GO2"))
    if wake == "force":
        return (_force("R"),), (_force("m"), _set("GO"), _set("GO2"))
    assert wake == "capacity"
    killjob = Event(at=T0, kind="KILLJOB", payload={"job": "h"})
    return (_force("h"), _force("R"), _set("GO")), (killjob, _set("GO2"))


def _m1_expected() -> dict[str, Any]:
    return {
        "during": {"m": ([], ("RUNNING", 1, False)), "m2": ([], ("RUNNING", 1, False))},
        "after": {"m": ([2], ("RUNNING", 2, False)), "m2": ([], ("INACTIVE", 1, True))},
    }


@pytest.mark.parametrize("wake", ["condition", "force", "capacity"])
@pytest.mark.parametrize("kind", ["command", "watch"])
def test_a_member_the_box_restart_ends_is_kept_back_and_started_once_in_the_new_box_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, wake: str
) -> None:
    """DL-527, DL-522, DL-463: the update adds `n` to the running box `R` and waits for `c`'s
    stop. `m`, an idle unchanged member of the sub-box `B`, is woken while it waits, by its
    condition, by FORCE_STARTJOB or by the release of the unit it queues for: its run opens and
    one `dispatch_pending` is recorded, with no launch and no `dispatch`. So for the marked
    `m2`. The box restart's kill ends run 1 of both while the update waits, and each kept start
    is discharged with that kill, with no launch (DL-551). After the application `m` is
    dispatched and launched once in the new box run under the applied revision, and `m2` stays
    held (DL-463)."""
    _Procs(monkeypatch)
    launches = _launches(monkeypatch)
    before, during = _wakes(wake)
    seen = asyncio.run(
        _admit(
            tmp_path,
            monkeypatch,
            launches,
            _nested(tmp_path, kind, wake, added=False),
            _nested(tmp_path, kind, wake, added=True),
            before=before,
            during=during,
            watch=("m", "m2"),
            marked=("m2",),
        )
    )
    assert {"m", "m2", "B"} <= seen["filter"]
    idle = "QUE_WAIT" if wake == "capacity" else "INACTIVE"
    assert seen["before"] == {"m": (idle, 0, False), "m2": ("INACTIVE", 0, False)}
    assert {key: seen[key] for key in ("during", "after")} == _m1_expected()
    assert _dispatch_revs(tmp_path, "m") == [(2, 2)]
    # the box restart's kill ends run 1 while the update waits: its start is discharged then
    # (DL-551)
    assert _starts(tmp_path, "m") == [
        ("pending", 1, False),
        ("discharged", None, False),
        ("dispatch", 2, True),
    ]
    assert _starts(tmp_path, "m2") == [("pending", 1, False), ("discharged", None, False)]


# ---------------------------------------------------------------- M2: the named set, and no more


def _moved(*, moved: bool) -> str:
    """`R` holds `c`, the command `x` and the sub-box `S`, which holds `d` and `t`, a command
    with a one-minute `term_run_time`; the unrelated `u` stands alone. With `moved`, `S` and a
    changed `x` leave `R`, which edits `R`."""
    text = _job("R", "job_type: b\n") + _job("c", _cmd(), "R") + _job("u", _cmd('v(GU) = "go"'))
    members = _job("d", _cmd('v(GD) = "go"'), "S") + _job(
        "t", _cmd('v(GT) = "go"', "term_run_time: 1\n"), "S"
    )
    if moved:
        return text + _job("x", _cmd('v(GX) = "go"')) + _job("S", "job_type: b\n") + members
    return text + _job("x", _cmd('v(GX) = "go"'), "R") + _job("S", "job_type: b\n", "R") + members


def test_a_moved_out_descendant_is_kept_back_and_launched_once_after_the_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-527, DL-522, DL-525: the update moves the sub-box `S` and the changed `x` out of the
    restarting box `R` and waits for `c`'s stop. Woken while it waits, the unrelated `u` launches
    at once. `d` and `t`, unchanged members of `S` and so descendants of `R` in the old catalog,
    and `x` are kept back: each run opens with one `dispatch_pending` and no launch. `t`'s
    `term_run_time` passes while it is held, so its run ends TERMINATED. After the application
    the restart has not ended `d`'s or `x`'s run: each is dispatched and launched once in that
    run, under the applied revision. `t`'s kept start is discharged once with no launch, by the
    input its runtime limit ended it in (DL-551)."""
    _Procs(monkeypatch)
    launches = _launches(monkeypatch)
    seen = asyncio.run(
        _admit(
            tmp_path,
            monkeypatch,
            launches,
            _moved(moved=False),
            _moved(moved=True),
            before=(_force("R"),),
            during=(_set("GU"), _set("GD"), _set("GX"), _set("GT")),
            watch=("u", "d", "x", "t"),
            advance=True,
        )
    )
    assert {"d", "t", "x", "S"} <= seen["filter"] and "u" not in seen["filter"]
    assert seen["during"] == {
        "u": ([1], ("RUNNING", 1, False)),
        "d": ([], ("RUNNING", 1, False)),
        "x": ([], ("RUNNING", 1, False)),
        "t": ([], ("TERMINATED", 1, False)),
    }
    assert seen["after"] == {
        "u": ([1], ("RUNNING", 1, False)),
        "d": ([1], ("RUNNING", 1, False)),
        "x": ([1], ("RUNNING", 1, False)),
        "t": ([], ("TERMINATED", 1, False)),
    }
    for job in ("d", "x"):
        assert _starts(tmp_path, job) == [("pending", 1, False), ("dispatch", 1, True)]
        assert _dispatch_revs(tmp_path, job) == [(1, 2)]
    assert _starts(tmp_path, "t") == [("pending", 1, False), ("discharged", None, False)]
    assert _starts(tmp_path, "u") == [("dispatch", 1, False)]


# ---------------------------------------------------------------- M3: a restart holds the same names


@pytest.mark.parametrize("source", ["tail", "snapshot"])
@pytest.mark.parametrize("kind", ["command", "watch"])
def test_a_restart_during_the_wait_holds_the_recorded_names_and_ends_as_the_uncut_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, source: str
) -> None:
    """DL-527, DL-432, DL-433: the engine dies after M1's pending records and before the
    application. The restart, from the whole tail or from a snapshot taken after the admission
    plus the tail, holds exactly the names the intent recorded: neither `m` nor `m2` launches
    while it waits. After the application the result is M1's."""
    _Procs(monkeypatch)
    launches = _launches(monkeypatch)
    before, during = _wakes("condition")
    first = asyncio.run(
        _admit(
            tmp_path,
            monkeypatch,
            launches,
            _nested(tmp_path, kind, "condition", added=False),
            _nested(tmp_path, kind, "condition", added=True),
            before=before,
            during=during,
            watch=("m", "m2"),
            marked=("m2",),
            crash=source,
        )
    )
    if source == "tail":
        drop_snapshots(tmp_path / "run")
    [intent] = [
        body
        for _seq, kind_, body in _journal(tmp_path)
        if (kind_, body.get("event")) == ("config", "intent")
    ]
    assert set(intent["filter"]) == first["filter"]
    seen = asyncio.run(_restart(tmp_path, monkeypatch, launches, first["c"], ("m", "m2")))
    assert seen["filter"] == first["filter"]
    assert seen["during"] == {"m": ([], ("RUNNING", 1, False)), "m2": ([], ("RUNNING", 1, False))}
    assert seen["after"] == _m1_expected()["after"]
    # the box restart's kill ends run 1 while the update waits: its start is discharged then
    # (DL-551)
    assert _starts(tmp_path, "m") == [
        ("pending", 1, False),
        ("discharged", None, False),
        ("dispatch", 2, True),
    ]
    assert _starts(tmp_path, "m2") == [("pending", 1, False), ("discharged", None, False)]


def test_a_root_whose_intent_records_the_narrower_filter_keeps_exactly_that_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-527, DL-489: an intent an earlier build wrote names only the plan's added, changed
    and removed jobs in its `filter`. The test narrows the recorded filter by hand to that set,
    as no build since writes it (DL-491). A restart holds exactly those names: woken after the
    restart, while the update still waits, `m2`, a member only the wider filter names, launches
    at once. Nothing widens the filter after the fact."""
    _Procs(monkeypatch)
    launches = _launches(monkeypatch)
    first = asyncio.run(
        _admit(
            tmp_path,
            monkeypatch,
            launches,
            _nested(tmp_path, "command", "condition", added=False),
            _nested(tmp_path, "command", "condition", added=True),
            before=(_force("R"),),
            during=(),
            watch=("m2",),
            crash="tail",
        )
    )
    assert first["during"] == {"m2": ([], ("INACTIVE", 0, False))}
    [(seq, intent)] = [
        (seq, body)
        for seq, kind, body in _journal(tmp_path)
        if (kind, body.get("event")) == ("config", "intent")
    ]
    assert "m2" in intent["filter"]
    rewrite(tmp_path / "run", seq, {**intent, "filter": ["n"]})
    drop_snapshots(tmp_path / "run")
    seen: dict[str, Any] = {}

    async def restart() -> None:
        ladders = _Ladders(monkeypatch)
        ladders.alive.add(PGID)
        _steps, looked = _update_steps(monkeypatch)
        launched = asyncio.Event()
        counted = Engine._launch

        def signalled(self: Engine, job_ir: Any, run_number: int, adapter: Any, **kw: Any) -> None:
            counted(self, job_ir, run_number, adapter, **kw)
            if job_ir.name == "m2":
                launched.set()

        monkeypatch.setattr(Engine, "_launch", signalled)
        at = T0 + timedelta(minutes=2)
        engine = await resume_run(
            prepare_estate(tmp_path / "estate").catalog,
            tmp_path / "run",
            clock=VirtualClock(start=at),
            adapters=_adapters(),
            settings=SETTINGS,
        )
        del launches[:]
        loop = asyncio.ensure_future(engine.run_until_quiescent(at))
        try:
            async with asyncio.timeout(30):  # a guard against a hang only
                await looked.wait()
                assert engine.update.tier is not None
                seen["filter"] = engine.update.tier.dispatch_filter
                engine.inject(
                    Event(at=at, kind="SET_GLOBAL", payload={"name": "GO2", "value": "go"})
                )
                await launched.wait()
            seen["launches"] = [launch[:2] for launch in launches]
            seen["stopping"] = (engine.update.stopping, engine.config_rev)
        finally:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            await _crash(engine)

    asyncio.run(restart())
    assert seen["filter"] == {"n"}
    assert seen["stopping"] == (True, 1)
    assert seen["launches"] == [("m2", 1)]


# ---------------------------------------------------------------- M4: a moved-out sub-box's member


def _terminators(*, moved: bool, flags: bool) -> str:
    """`R` holds `c` and the sub-box `S`, which holds the command `J`; with `flags`, `S` and
    `J` are `job_terminator` members. With `moved`, `S` leaves `R` without its flag."""
    flag = "job_terminator: 1\n" if flags else ""
    text = _job("R", "job_type: b\n") + _job("c", _cmd(), "R")
    s = _job("S", "job_type: b\n") if moved else _job("S", "job_type: b\n" + flag, "R")
    return text + s + _job("J", _cmd('v(GJ) = "go"', flag), "S")


@pytest.mark.parametrize("flags", [True, False])
def test_a_member_of_a_moved_out_sub_box_is_kept_back_whatever_its_terminator_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: bool
) -> None:
    """DL-527, DL-522: the update moves the sub-box `S` out of the restarting box `R`. `J`, an
    unchanged member of `S` and so a descendant of `R` in the old catalog, is woken while the
    update waits and is kept back, with or without `job_terminator` on `S` and `J`. With the
    flags, the KILLJOB of `R` under the old catalog ends `S` and `J` (SEM-14, DL-348): `J`'s kept
    start is discharged with that kill, before the application, and it is not launched
    (DL-551). Without them its run survives the restart, and it is launched once in that run
    after the application."""
    _Procs(monkeypatch)
    launches = _launches(monkeypatch)
    seen = asyncio.run(
        _admit(
            tmp_path,
            monkeypatch,
            launches,
            _terminators(moved=False, flags=flags),
            _terminators(moved=True, flags=flags),
            before=(_force("R"),),
            during=(_set("GJ"),),
            watch=("J",),
        )
    )
    assert "J" in seen["filter"]
    assert seen["during"] == {"J": ([], ("RUNNING", 1, False))}
    if flags:
        assert seen["after"] == {"J": ([], ("TERMINATED", 1, False))}
        assert _starts(tmp_path, "J") == [("pending", 1, False), ("discharged", None, False)]
    else:
        assert seen["after"] == {"J": ([1], ("RUNNING", 1, False))}
        assert _starts(tmp_path, "J") == [("pending", 1, False), ("dispatch", 1, True)]


# ---------------------------------------------------------------- the limit


def test_a_terminator_member_of_an_outer_box_the_update_does_not_name_launches_and_is_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-527 THE LIMIT, SEM-14: the restarting box `R` is a `box_terminator` member of the
    running box `Q`, which the update does not edit. `y`, an unchanged `job_terminator` member
    of `Q`, is outside the named set: woken while the update waits, it launches at once. The
    KILLJOB of `R` under the old catalog then ends `Q`, and `Q`'s end ends `y`."""
    _Procs(monkeypatch)
    launches = _launches(monkeypatch)

    def estate(*, after: bool) -> str:
        text = _job("Q", "job_type: b\n") + _job("R", "job_type: b\nbox_terminator: 1\n", "Q")
        text += _job("c", _cmd(), "R")
        text += _job("y", _cmd('v(GY) = "go"', "job_terminator: 1\n"), "Q")
        return text + (_job("n", _cmd(), "R") if after else "")

    seen = asyncio.run(
        _admit(
            tmp_path,
            monkeypatch,
            launches,
            estate(after=False),
            estate(after=True),
            before=(_force("Q"),),
            during=(_set("GY"),),
            watch=("y",),
        )
    )
    assert "y" not in seen["filter"]
    assert seen["during"] == {"y": ([1], ("RUNNING", 1, False))}
    assert seen["after"] == {"y": ([1], ("TERMINATED", 1, False))}
