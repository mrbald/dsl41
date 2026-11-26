"""While an update waits, no process is launched for a job its recorded dispatch filter names,
and a restart holds exactly the names recorded (DL-522, DL-525, DL-527). The named jobs are the
plan's added, changed and removed jobs (DL-559). A start the oracle decides for one waits as
`dispatch_pending`. One whose run ends while it waits is discharged with the input that ends it
(DL-551). After the application the rest are released: discharged with no launch when the run
is no longer wanted, else launched under the applied definition.

The scenarios run on the virtual clock with `test_engine_stops_cookie`'s process stand-ins. The
update waits for `c`, a running command whose stop sequence the test parks and ends
(`test_update_atomic._Ladders`). Each `Engine._launch` is counted. `test_wanted_starts` and
`test_explain_causes` reuse the helpers.
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


# ---------------------------------------------------------------- a restart holds the recorded names


def test_a_root_whose_intent_records_the_narrower_filter_keeps_exactly_that_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-527, DL-559: an update changes the running `c` and the idle `m2`, so its recorded
    `filter` names both. The test narrows the recorded filter by hand to `c` (DL-491). A restart
    holds exactly the names recorded: woken after the restart, while the update still waits,
    `m2` launches at once. Nothing widens the filter after the fact."""
    _Procs(monkeypatch)
    launches = _launches(monkeypatch)
    old = _job("c", _cmd()) + _job("m2", _cmd('v(GO2) = "go"'))
    new = _job("c", _cmd(extra="description: changed\n")) + _job(
        "m2", _cmd('v(GO2) = "go"', "description: changed\n")
    )
    first = asyncio.run(
        _admit(
            tmp_path,
            monkeypatch,
            launches,
            old,
            new,
            before=(_force("c"),),
            during=(),
            watch=("m2",),
            crash="tail",
        )
    )
    assert first["filter"] == {"c", "m2"}
    assert first["during"] == {"m2": ([], ("INACTIVE", 0, False))}
    [(seq, intent)] = [
        (seq, body)
        for seq, kind, body in _journal(tmp_path)
        if (kind, body.get("event")) == ("config", "intent")
    ]
    assert intent["filter"] == ["c", "m2"]
    rewrite(tmp_path / "run", seq, {**intent, "filter": ["c"]})
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
    assert seen["filter"] == {"c"}
    assert seen["stopping"] == (True, 1)
    assert seen["launches"] == [("m2", 1)]
