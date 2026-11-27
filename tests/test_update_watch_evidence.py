"""A file watch's evidence belongs to its attempt, and an attempt to one definition (DL-566).

A watch an update changed since its attempt's dispatch is replaced where it is taken up, after
the application or at a restart: its attempt is closed once as stopped by the update, with the
interruption rule's plan, and a new attempt of the same run watches under the applied rev with
its own spool and an empty log. An unchanged watch goes on with its attempt, its log and its
streak. "Changed" is what the committed `config applied` records name in `changed`.

The scenarios run on the virtual clock. `w` is a file watch; `v`, where present, is a watch an
update changes while it leaves `w` alone. `a` and `b` are five-byte files, so `b` has `a`'s
last size; `gone` never exists.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from reload_fixtures import prepare_estate
from store_surgery import rows
from test_engine_stops_cookie import T0
from test_update_owned_processes import _crash
from test_update_settlement import _close, _plan
from test_update_watches import _estate, _launches, _resume, _rewrite, _start

from dsl41.oracle_state import Event
from dsl41.reload_update import Admitted
from dsl41.runner import Engine
from dsl41.runner_adapters import WATCH_LOG
from dsl41.runner_history import read_run_root
from dsl41.runner_journal import STORE_NAME
from dsl41.runner_runs import Request, Restored, answer
from dsl41.runner_store import SqliteStore
from dsl41.reread_worker import preview
from dsl41.runner_update import EngineUpdate, UpdateRequest

#: the watch interval
EVERY = timedelta(seconds=60)
#: what `a` and `b` hold: five bytes each
FIVE = "abcd\n"


class _Died(Exception):
    """The engine dies inside the loop."""


def _fw(target: Path, name: str = "w", interval: int = 60) -> str:
    return f"insert_job: {name}\njob_type: f\nwatch_file: {target}\nwatch_interval: {interval}\n\n"


def _files(tmp_path: Path) -> dict[str, Path]:
    """`a` and `b`, five bytes each, a directory `d` holding `d/a1` of five bytes, and `gone`."""
    (tmp_path / "a").write_text(FIVE)
    (tmp_path / "b").write_text(FIVE)
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a1").write_text(FIVE)
    return {
        "a": tmp_path / "a",
        "b": tmp_path / "b",
        "gone": tmp_path / "gone",
        "a*": tmp_path / "d" / "a*",
        "b*": tmp_path / "d" / "b*",
        "d/b": tmp_path / "d" / "b",
    }


async def _update(engine: Engine, tmp_path: Path, text: str, existence: str, rid: str) -> Any:
    """Apply `text` under `existence` as one update; answer its decision."""
    _estate(tmp_path, text, existence)
    token = _plan(engine, tmp_path)
    request = UpdateRequest(token, engine.config_rev, engine.baseline_id, rid)
    answer = engine.request_update(request)
    await _until(engine, engine.clock.now())
    return answer.result().decision


async def _started(tmp_path: Path, text: str, existence: str = "stable") -> Engine:
    """An engine on `text` whose `w` (and `v`, if any) was started at T0 and polled once."""
    engine = _start(tmp_path, text, existence)
    for job in engine.oracle.catalog.jobs:
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
    await _until(engine, T0)
    return engine


async def _until(engine: Engine, at: datetime) -> None:
    """Run the engine to `at`, every poll due before it at its own instant, the clock at `at`."""
    await engine.run_until_quiescent(at)
    await engine.clock.wait_until(at)
    await engine.run_until_quiescent(at)


async def _stop(engine: Engine) -> None:
    await engine.shutdown()
    _close(engine)


def _status(engine: Engine, job: str = "w") -> str:
    return engine.oracle.store.job[job].status


def _journal(tmp_path: Path) -> list[tuple[int, str, dict[str, Any]]]:
    return rows(tmp_path / "run")


def _attempts(tmp_path: Path, job: str = "w") -> tuple[list[str], list[tuple[int, int, str]]]:
    """Each `dispatch` of `job`: the attempt ids, and each one's ordinal, rev and spool
    relative to the run root."""
    root = tmp_path / "run"
    found = [b for _, k, b in _journal(tmp_path) if k == "dispatch" and b["job"] == job]
    return [b["attempt"] for b in found], [
        (b["ordinal"], b["rev"], str(Path(b["spool"]).relative_to(root))) for b in found
    ]


def _ends(tmp_path: Path, attempt: str) -> list[tuple[str, str | None]]:
    """The `outcome` (with its cause) and `follow_up` plan records of `attempt`."""
    return [
        (b["outcome"], b.get("cause")) if k == "outcome" else ("plan", b["plan"])
        for _, k, b in _journal(tmp_path)
        if b.get("attempt") == attempt
        and (k == "outcome" or (k == "follow_up" and b.get("event") == "plan"))
    ]


def _stopped(tmp_path: Path) -> list[list[list[str]]]:
    """Each update's `config applied.stopped`, in order."""
    return [
        b["stopped"]
        for _, k, b in _journal(tmp_path)
        if (k, b.get("event")) == ("config", "applied") and "token" in b
    ]


def _lines(tmp_path: Path, spool: str) -> list[str]:
    """The kinds of the lines of the watch log in `spool`."""
    log = tmp_path / "run" / spool / WATCH_LOG
    return [json.loads(line)["kind"] for line in log.read_bytes().splitlines()]


def _history(tmp_path: Path) -> dict[str, Any]:
    """The `history` answer for `w`, read from the stopped root's store."""
    with closing(SqliteStore(tmp_path / "run" / STORE_NAME, read_only=True)) as store:
        request = Request("w", None, 20, None)
        return answer(store, request, upto=store.last_seq(), header={}, restored=Restored(None, []))


def _dies_before_finish(monkeypatch: pytest.MonkeyPatch, *, snapshot: bool = False) -> None:
    """The engine dies after the `config applied` record and its time advance, before the
    update's finish takes the watch up; with `snapshot`, once a snapshot covers the record."""

    def finish(self: EngineUpdate) -> None:
        if snapshot:
            self.engine.take_snapshot()
        raise _Died

    monkeypatch.setattr(EngineUpdate, "finish", finish)


# ---------------------------------------------------------------- the four reproductions


def test_dl566_a_changed_target_with_the_old_size_needs_two_looks_at_the_new_one(
    tmp_path: Path,
) -> None:
    """Case 1: `w` sees `a` once, five bytes. At T0+30s an update moves it to `b`, also five
    bytes. One look at `b` completes nothing: the update closed attempt one, stopped by the
    update with plan `rerun`, and attempt two watches `b` from an empty log in its own
    directory under rev 2. It completes at its second look, T0+90s."""
    paths = _files(tmp_path)

    async def scenario() -> list[str]:
        engine = await _started(tmp_path, _fw(paths["a"]))
        seen = []
        try:
            await _until(engine, T0 + EVERY / 2)
            decision = await _update(engine, tmp_path, _fw(paths["b"]), "stable", "u1")
            assert isinstance(decision, Admitted) and decision.stop_set == ("w",)
            for at in (T0 + EVERY, T0 + EVERY * 3 / 2):
                await _until(engine, at)
                seen.append(_status(engine))
        finally:
            await _stop(engine)
        return seen

    assert asyncio.run(scenario()) == ["RUNNING", "SUCCESS"]
    (first, second), shapes = _attempts(tmp_path)
    assert shapes == [(1, 1, "runs/w.1"), (2, 2, "runs/w.1/2")]
    assert _ends(tmp_path, first) == [("stopped", "update_stop"), ("plan", "rerun")]
    assert _ends(tmp_path, second) == [("natural", None), ("plan", "complete")]
    assert _stopped(tmp_path) == [[["w", "rerun"]]]
    assert _lines(tmp_path, "runs/w.1") == ["start", "poll"]
    assert _lines(tmp_path, "runs/w.1/2") == ["start", "poll", "poll"]
    # `history` lists both attempts of the run with their outcomes; `runs` keeps one row
    page = _history(tmp_path)
    [run] = page["runs"]
    assert (run["row"]["run_number"], run["row"]["status"], run["row"]["duration_s"]) == (
        1,
        "SUCCESS",
        90.0,
    )
    assert [
        (a["ordinal"], a["attempt"], a["rev"], a["after"], a["outcome"]["kind"])
        for a in run["attempts"]
    ] == [(1, first, 1, None, "stopped"), (2, second, 2, "update_stop", "natural")]
    assert [a["spool"] for a in run["attempts"]] == [
        str(tmp_path / "run" / "runs" / "w.1"),
        str(tmp_path / "run" / "runs" / "w.1" / "2"),
    ]
    [row] = [row for row in read_run_root(tmp_path / "run") if row.job == "w"]
    assert (row.run_number, row.status, row.exit_code) == (1, "SUCCESS", 0)
    assert row.run_dir == str(tmp_path / "run" / "runs" / "w.1" / "2")


@pytest.mark.parametrize("target", ["exact", "wildcard"])
def test_dl566_an_update_to_immediate_never_completes_from_the_old_targets_first_poll(
    tmp_path: Path, target: str
) -> None:
    """Case 2: `w` sees `a` (or `d/a1` through `d/a*`) at its first poll. An update makes the
    estate `fw-existence=immediate` and moves `w` to `gone` (or `d/b*`), which does not exist.
    The watch does not complete: attempt two polls the new target again and again and finds
    nothing."""
    paths = _files(tmp_path)
    old, new = ("a", "gone") if target == "exact" else ("a*", "b*")

    async def scenario() -> list[str]:
        engine = await _started(tmp_path, _fw(paths[old]))
        seen = []
        try:
            await _until(engine, T0 + EVERY / 2)
            await _update(engine, tmp_path, _fw(paths[new]), "immediate", "u1")
            seen.append(_status(engine))
            await _until(engine, T0 + EVERY * 3)
            seen.append(_status(engine))
        finally:
            await _stop(engine)
        return seen

    assert asyncio.run(scenario()) == ["RUNNING", "RUNNING"]
    (first, second), shapes = _attempts(tmp_path)
    assert shapes == [(1, 1, "runs/w.1"), (2, 2, "runs/w.1/2")]
    assert _ends(tmp_path, first) == [("stopped", "update_stop"), ("plan", "rerun")]
    assert _ends(tmp_path, second) == []
    kind = "poll" if target == "exact" else "sample"
    assert _lines(tmp_path, "runs/w.1/2") == ["start"] + [kind] * 3  # T0+30s, 90s, 150s


@pytest.mark.parametrize("snapshot", [False, True])
def test_dl566_a_death_after_the_application_replaces_the_watch_at_the_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot: bool
) -> None:
    """Case 3: as in case 2, but the engine dies after `config applied` and before the update's
    finish takes `w` up. The restart finds `w`'s attempt open and no update open. It does not
    read the old log for a completion: it closes attempt one, stopped by the update with plan
    `rerun`, and dispatches attempt two under rev 2, which finds nothing at `gone`. With
    `snapshot`, a snapshot covers the `config applied` record, which the restart reads from
    before it."""
    paths = _files(tmp_path)

    async def first() -> None:
        engine = await _started(tmp_path, _fw(paths["a"]))
        try:
            await _until(engine, T0 + EVERY / 2)
            with monkeypatch.context() as patch:
                _dies_before_finish(patch, snapshot=snapshot)
                with pytest.raises(_Died):
                    await _update(engine, tmp_path, _fw(paths["gone"]), "immediate", "u1")
        finally:
            await _crash(engine)

    asyncio.run(first())
    ([attempt], _shapes) = _attempts(tmp_path)
    assert _ends(tmp_path, attempt) == []
    snapshots = [
        b for _, k, b in _journal(tmp_path) if (k, b.get("event")) == ("engine", "snapshot")
    ]
    assert len(snapshots) == int(snapshot)
    launches = _launches(monkeypatch)

    async def restart() -> list[str]:
        at = T0 + EVERY
        engine = await _resume(tmp_path, at, "immediate")
        try:
            await _until(engine, at)
            seen = [_status(engine)]
            await _until(engine, at + EVERY * 2)
            return seen + [_status(engine)]
        finally:
            await _stop(engine)

    assert asyncio.run(restart()) == ["RUNNING", "RUNNING"]
    (first_id, second), shapes = _attempts(tmp_path)
    assert first_id == attempt and shapes == [(1, 1, "runs/w.1"), (2, 2, "runs/w.1/2")]
    assert _ends(tmp_path, attempt) == [("stopped", "update_stop"), ("plan", "rerun")]
    assert [launch[:3] for launch in launches] == [("w", 1, second)]
    assert _lines(tmp_path, "runs/w.1/2") == ["start", "poll", "poll", "poll"]


def test_dl566_a_to_b_to_a_counts_no_look_at_b_toward_a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 4: `w` watches `a`, absent at its first poll. An update moves it to `b`, which
    attempt two sees once; `a` then appears, and a second update moves `w` back to `a`. The
    engine dies before the second update's finish. The open attempt's definition (rev 2's)
    differs from the applied one (rev 3's), but rev 3's equals rev 1's: only the second
    `config applied` says `w` changed since attempt two's dispatch. The restart replaces
    attempt two, so `b`'s five bytes are no look at `a`: attempt three needs two."""
    paths = _files(tmp_path)
    (tmp_path / "a").unlink()

    async def first() -> None:
        engine = await _started(tmp_path, _fw(paths["a"]))
        try:
            await _until(engine, T0 + EVERY / 2)
            await _update(engine, tmp_path, _fw(paths["b"]), "stable", "u1")
            await _until(engine, T0 + EVERY)
            assert _status(engine) == "RUNNING"  # attempt two saw `b` once, at T0+30s
            (tmp_path / "a").write_text(FIVE)
            with monkeypatch.context() as patch:
                _dies_before_finish(patch)
                with pytest.raises(_Died):
                    await _update(engine, tmp_path, _fw(paths["a"]), "stable", "u2")
        finally:
            await _crash(engine)

    asyncio.run(first())

    async def restart() -> list[str]:
        at = T0 + timedelta(seconds=110)
        engine = await _resume(tmp_path, at, "stable")
        try:
            seen = []
            for when in (at, T0 + timedelta(seconds=125), T0 + timedelta(seconds=175)):
                await _until(engine, when)
                seen.append(_status(engine))
            return seen
        finally:
            await _stop(engine)

    # attempt two's next look would be at T0+120s; attempt three looks at T0+110s and T0+170s
    assert asyncio.run(restart()) == ["RUNNING", "RUNNING", "SUCCESS"]
    (_one, two, three), shapes = _attempts(tmp_path)
    assert shapes == [(1, 1, "runs/w.1"), (2, 2, "runs/w.1/2"), (3, 3, "runs/w.1/3")]
    assert _lines(tmp_path, "runs/w.1/2") == ["start", "poll"]
    assert _ends(tmp_path, two) == [("stopped", "update_stop"), ("plan", "rerun")]
    assert _lines(tmp_path, "runs/w.1/3") == ["start", "poll", "poll"]
    assert _ends(tmp_path, three) == [("natural", None), ("plan", "complete")]


# ---------------------------------------------------------------- what goes on unchanged


def test_dl566_an_unchanged_watch_keeps_its_attempt_log_and_streak_across_update_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`w` sees `a` once at T0. An update changes `v` only: `w`'s task runs on, the update
    stops nothing of it and names it nowhere. The engine dies; the restart resumes `w` under
    the same attempt id from the same log, so the look at T0+60s is the streak's second and
    completes it. `v` is replaced by the update and resumed by the restart as attempt two."""
    paths = _files(tmp_path)
    text = _fw(paths["a"]) + _fw(paths["gone"], "v")

    async def first() -> str:
        engine = await _started(tmp_path, text)
        try:
            watch = engine._live["w"].task
            await _until(engine, T0 + EVERY / 4)
            changed = _fw(paths["a"]) + _fw(paths["gone"], "v", interval=61)
            decision = await _update(engine, tmp_path, changed, "stable", "u1")
            assert isinstance(decision, Admitted) and decision.stop_set == ("v",)
            assert engine._live["w"].task is watch and not watch.done()
            await _until(engine, T0 + EVERY / 2)
            return str(engine._live["w"].attempt)
        finally:
            await _crash(engine)

    attempt = asyncio.run(first())
    launches = _launches(monkeypatch)

    async def restart() -> str:
        at = T0 + EVERY * 3 / 4
        engine = await _resume(tmp_path, at, "stable")
        try:
            await _until(engine, at)
            assert _status(engine) == "RUNNING"
            await _until(engine, T0 + EVERY)
            return _status(engine)
        finally:
            await _stop(engine)

    assert asyncio.run(restart()) == "SUCCESS"
    assert _attempts(tmp_path) == ([attempt], [(1, 1, "runs/w.1")])
    assert _ends(tmp_path, attempt) == [("natural", None), ("plan", "complete")]
    assert _lines(tmp_path, "runs/w.1") == ["start", "poll", "poll"]
    (_v1, v2), shapes = _attempts(tmp_path, "v")
    assert shapes == [(1, 1, "runs/v.1"), (2, 2, "runs/v.1/2")]
    assert sorted(launch[:3] for launch in launches) == [("v", 1, v2), ("w", 1, attempt)]
    assert _stopped(tmp_path) == [[["v", "rerun"]]]


def test_dl566_n_then_an_unchanged_update_then_a_changing_one_with_a_restart_at_each_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`w` watches `gone` from T0, and the engine dies and restarts at each point: after the
    start (rev 1), after an update that changes `v` only (rev 2), and after one that changes
    `w` (rev 3). The first two restarts resume `w`'s first attempt; the third update replaces
    it, and the last restart resumes the replacement, which rev 3 dispatched."""
    paths = _files(tmp_path)
    launches = _launches(monkeypatch)
    texts = [
        _fw(paths["gone"]) + _fw(paths["gone"], "v"),
        _fw(paths["gone"]) + _fw(paths["gone"], "v", interval=61),
        _fw(paths["gone"], interval=62) + _fw(paths["gone"], "v", interval=61),
    ]

    async def first() -> None:
        engine = await _started(tmp_path, texts[0])
        await _crash(engine)

    async def restart(step: int) -> None:
        at = T0 + EVERY * step
        engine = await _resume(tmp_path, at, "stable")
        try:
            await _until(engine, at)
            assert _status(engine) == "RUNNING"
            if step < len(texts):
                await _update(engine, tmp_path, texts[step], "stable", f"u{step}")
                assert engine.config_rev == step + 1
        finally:
            await _crash(engine)

    asyncio.run(first())
    for step in (1, 2, 3):
        asyncio.run(restart(step))
    (w1, w2), shapes = _attempts(tmp_path)
    assert shapes == [(1, 1, "runs/w.1"), (2, 3, "runs/w.1/2")]
    assert [launch[2] for launch in launches if launch[0] == "w"] == [w1, w1, w1, w2, w2]
    assert _ends(tmp_path, w1) == [("stopped", "update_stop"), ("plan", "rerun")]
    assert _ends(tmp_path, w2) == []
    assert _stopped(tmp_path) == [[["v", "rerun"]], [["w", "rerun"]]]


# ---------------------------------------------------------------- the replacement


def test_dl566_a_death_between_the_close_and_the_new_dispatch_still_yields_the_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine dies after the update committed `w`'s outcome with its `rerun` plan, before
    the replacement's `dispatch`. The restart performs the owed plan: the replacement is
    dispatched once, under the attempt id the plan named, with the plan's step marked done."""
    paths = _files(tmp_path)
    spawn = Engine.spawn

    def dying(self: Engine, job: str, run_number: int, **kwargs: Any) -> bool:
        if kwargs.get("attempt") is not None:
            raise _Died
        return spawn(self, job, run_number, **kwargs)

    async def first() -> None:
        engine = await _started(tmp_path, _fw(paths["a"]))
        try:
            with monkeypatch.context() as patch:
                patch.setattr(Engine, "spawn", dying)
                with pytest.raises(_Died):
                    await _update(engine, tmp_path, _fw(paths["b"]), "stable", "u1")
        finally:
            await _crash(engine)

    asyncio.run(first())
    ([attempt], _shapes) = _attempts(tmp_path)
    assert _ends(tmp_path, attempt) == [("stopped", "update_stop"), ("plan", "rerun")]
    [plan] = [b for _, k, b in _journal(tmp_path) if k == "follow_up" and b["event"] == "plan"]
    [step] = plan["steps"]
    assert (step["step"], step["ordinal"]) == ("dispatch", 2)

    async def restart() -> str:
        at = T0 + EVERY / 2
        engine = await _resume(tmp_path, at, "stable")
        try:
            await _until(engine, at + EVERY)
            return _status(engine)
        finally:
            await _stop(engine)

    assert asyncio.run(restart()) == "SUCCESS"
    (_first, second), shapes = _attempts(tmp_path)
    assert second == step["attempt"] and shapes[1] == (2, 2, "runs/w.1/2")
    done = [b for _, k, b in _journal(tmp_path) if k == "follow_up" and b["event"] == "done"]
    assert [b["attempt"] for b in done] == [attempt, second]
    assert _lines(tmp_path, "runs/w.1/2") == ["start", "poll", "poll"]


def test_dl566_the_fifth_replacement_holds_the_watch_and_the_record_says_hold(
    tmp_path: Path,
) -> None:
    """Each of five updates changes `w`'s interval. The first four replace it, attempts two
    to five; the fifth interrupts attempt five, the limit, so the interruption rule holds the
    watch as it holds a command (DL-485): `config applied.stopped` says `hold`, the plan is a
    hold with the limit in its evidence, and no sixth attempt is dispatched. `reread`'s
    forecast, taken just before each update with no event between, says what the application
    then records: `rerun` four times, then `hold`."""
    paths = _files(tmp_path)

    async def scenario() -> tuple[list[list[dict[str, str]]], str, bool]:
        engine = await _started(tmp_path, _fw(paths["gone"]))
        forecasts = []
        try:
            for n in range(1, 6):
                text = _fw(paths["gone"], interval=60 + n)
                _estate(tmp_path, text, "stable")
                candidate = prepare_estate(tmp_path / "estate")
                forecasts.append(preview(candidate, engine.update.live())["effects"])
                await _update(engine, tmp_path, text, "stable", f"u{n}")
            row = engine.oracle.store.job["w"]
            return forecasts, row.status, row.on_hold
        finally:
            await _stop(engine)

    forecasts, status, held = asyncio.run(scenario())
    rerun, hold_ = [{"job": "w", "action": "rerun"}], [{"job": "w", "action": "hold"}]
    assert forecasts == [rerun] * 4 + [hold_]
    assert (status, held) == ("FAILURE", True)
    ids, shapes = _attempts(tmp_path)
    assert [shape[:2] for shape in shapes] == [(n, n) for n in range(1, 6)]
    assert _stopped(tmp_path) == [[["w", "rerun"]]] * 4 + [[["w", "hold"]]]
    assert _ends(tmp_path, ids[-1]) == [("stopped", "update_stop"), ("plan", "hold")]
    [hold] = [
        b
        for _, k, b in _journal(tmp_path)
        if k == "follow_up" and b.get("event") == "plan" and b["plan"] == "hold"
    ]
    assert hold["evidence"]["limit"] == 5 and hold["evidence"]["run_id"] == ids[-1]


def test_dl566_a_replacement_under_immediate_needs_two_looks_also_after_a_death_after_its_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under `fw-existence=immediate`, `w`'s first attempt finds `gone` absent at its first
    poll. An update moves it to `b`, which exists: attempt two's first poll qualifies, and it
    does not complete, since only attempt one has the run's first poll (DL-258). The engine
    dies right after that poll; the restart reads the log, does not complete it either, and
    resumes attempt two, which completes at its second look."""
    paths = _files(tmp_path)

    async def first() -> str:
        engine = await _started(tmp_path, _fw(paths["gone"]), "immediate")
        try:
            await _until(engine, T0 + EVERY / 2)
            await _update(engine, tmp_path, _fw(paths["b"]), "immediate", "u1")
            await _until(engine, T0 + EVERY * 3 / 4)
            return _status(engine)
        finally:
            await _crash(engine)

    assert asyncio.run(first()) == "RUNNING"
    (_first, second), _shapes = _attempts(tmp_path)
    assert _lines(tmp_path, "runs/w.1/2") == ["start", "poll"]
    launches = _launches(monkeypatch)

    async def restart() -> list[str]:
        at = T0 + EVERY
        engine = await _resume(tmp_path, at, "immediate")
        try:
            await _until(engine, at)
            seen = [_status(engine)]
            await _until(engine, T0 + EVERY * 3 / 2)
            return seen + [_status(engine)]
        finally:
            await _stop(engine)

    assert asyncio.run(restart()) == ["RUNNING", "SUCCESS"]
    assert [launch[:3] for launch in launches] == [("w", 1, second)]
    assert _lines(tmp_path, "runs/w.1/2") == ["start", "poll", "poll"]
    assert _ends(tmp_path, second) == [("natural", None), ("plan", "complete")]


def test_dl566_a_changed_watch_whose_log_names_another_run_is_still_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-526, DL-118: as in case 3 the engine dies before the update's finish, and `w`'s log
    is then rewritten to name another run. The restart still classifies the log for identity
    first: the attempt is quarantined and held, and no replacement is dispatched."""
    paths = _files(tmp_path)

    async def first() -> None:
        engine = await _started(tmp_path, _fw(paths["a"]))
        try:
            with monkeypatch.context() as patch:
                _dies_before_finish(patch)
                with pytest.raises(_Died):
                    await _update(engine, tmp_path, _fw(paths["b"]), "stable", "u1")
        finally:
            await _crash(engine)

    asyncio.run(first())
    _rewrite(tmp_path, "someone-else", [])

    async def restart() -> None:
        at = T0 + EVERY
        engine = await _resume(tmp_path, at, "stable")
        try:
            await _until(engine, at + EVERY)
        finally:
            await _stop(engine)

    asyncio.run(restart())
    ([attempt], _shapes) = _attempts(tmp_path)
    assert _ends(tmp_path, attempt) == [("unobservable", "quarantined"), ("plan", "hold")]


def test_dl566_status_shows_the_replacement_as_the_live_watch_with_no_attempt_note(
    tmp_path: Path,
) -> None:
    """What `status` and the terminal UI read of a run with two watch attempts: the row stays
    RUNNING under its run number, `watching` names the replacement's file and interval, and
    the attempt column stays empty, as for any watch (`attempt_view` reads observed attempts
    only)."""
    paths = _files(tmp_path)

    async def scenario() -> tuple[Any, ...]:
        engine = await _started(tmp_path, _fw(paths["gone"]))
        try:
            await _update(engine, tmp_path, _fw(paths["gone"], interval=90), "stable", "u1")
            row = engine.oracle.store.job["w"]
            (_first, second), _shapes = _attempts(tmp_path)
            return (
                row.status,
                row.run_number,
                engine._live["w"].attempt == second,
                engine.watching_interval("w"),
                engine.attempt_view("w"),
            )
        finally:
            await _stop(engine)

    assert asyncio.run(scenario()) == ("RUNNING", 1, True, 90, None)


# ---------------------------------------------------------------- the replacement's spool


def _syncs(monkeypatch: pytest.MonkeyPatch, root: Path, dies_at: str | None = None) -> list[str]:
    """Every directory the FW adapter syncs in the run root from now on, relative to it (the
    root itself as `.`), in order. With `dies_at`, the engine dies at the first sync of that
    directory, before it is synced."""
    from dsl41 import runner_adapters

    seen: list[str] = []
    real = runner_adapters.fsync_dir

    def recorded(path: Path) -> None:
        if path == root or root in path.parents:
            name = str(path.relative_to(root))
            if name == dies_at:
                raise _Died
            seen.append(name)
        real(path)

    monkeypatch.setattr(runner_adapters, "fsync_dir", recorded)
    return seen


def test_dl566_a_replacement_syncs_its_new_directory_where_attempt_one_made_the_run_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The usual order. Each launch syncs the parent of every directory from its spool up to
    `runs/`, the deepest first, then its spool once its start line landed: attempt one syncs
    `runs` and the root, then `runs/w.1`; the replacement syncs `runs/w.1`, `runs` and the root,
    then `runs/w.1/2`."""
    paths = _files(tmp_path)
    seen = _syncs(monkeypatch, tmp_path / "run")

    async def scenario() -> list[str]:
        engine = await _started(tmp_path, _fw(paths["gone"]))
        try:
            first = list(seen)
            await _update(engine, tmp_path, _fw(paths["gone"], interval=61), "stable", "u1")
            return first
        finally:
            await _stop(engine)

    first = asyncio.run(scenario())
    assert first == ["runs", ".", "runs/w.1"]
    assert seen[len(first) :] == ["runs/w.1", "runs", ".", "runs/w.1/2"]


def test_dl566_a_replacement_made_before_its_run_s_directory_syncs_runs_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the real clock, one turn of the loop takes `w`'s start and an update that changes
    `w`: the update cancels attempt one before its task's first step, so attempt one makes no
    directory. The replacement makes `runs/w.1` and `runs/w.1/2`. It syncs the parent of every
    directory up to `runs/`, the deepest first: `runs/w.1`, `runs/`, which owns the new entry,
    and the root; then `runs/w.1/2` once its start line landed."""
    from test_update_wiring import TIMEOUT_S

    from dsl41 import main_config as main
    from dsl41.runner_clock import RealClock
    from dsl41.runner_config import EngineSettings
    from dsl41.runner_startup import start_run
    from test_update_watches import _adapters

    paths = _files(tmp_path)
    root = tmp_path / "run"
    seen = _syncs(monkeypatch, root)

    async def scenario() -> None:
        estate = prepare_estate(_estate(tmp_path, _fw(paths["gone"]), "stable"))
        engine = start_run(
            estate.catalog,
            root,
            source_capture=main.encoded_parts(estate),
            launched=main.profile_of(estate),
            clock=RealClock(),
            adapters=_adapters("stable"),
            settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
            hold_open=True,
        )
        _estate(tmp_path, _fw(paths["gone"], interval=61), "stable")
        token = _plan(engine, tmp_path)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        try:
            # one turn of the loop takes the two, in this order
            engine.inject(
                Event(at=engine.now_present(), kind="FORCE_STARTJOB", payload={"job": "w"})
            )
            answer = engine.request_update(UpdateRequest(token, 1, engine.baseline_id, "u1"))
            async with asyncio.timeout(TIMEOUT_S):  # a guard against a hang only
                await (await answer).applied
                # the adapter syncs the log's directory in the step that appends its start line
                while not (root / "runs" / "w.1" / "2" / WATCH_LOG).exists():
                    await asyncio.sleep(0.01)
        finally:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    assert not (root / "runs" / "w.1" / WATCH_LOG).exists()  # attempt one wrote nothing
    (first, _second), shapes = _attempts(tmp_path)
    assert shapes == [(1, 1, "runs/w.1"), (2, 2, "runs/w.1/2")]
    assert _ends(tmp_path, first) == [("stopped", "update_stop"), ("plan", "rerun")]
    assert seen == ["runs/w.1", "runs", ".", "runs/w.1/2"]


def test_dl566_a_launch_cut_short_before_its_syncs_leaves_them_to_the_next_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replacement made `runs/w.1/2` and synced `runs/w.1`, and the engine died before it
    synced `runs/`. At the restart both directories exist; the resumed launch of attempt two
    syncs the whole chain all the same, `runs/w.1`, `runs/` and the root, before it relies on
    the log, then `runs/w.1/2` once its start line landed."""
    paths = _files(tmp_path)
    root = tmp_path / "run"

    async def first() -> list[str]:
        engine = await _started(tmp_path, _fw(paths["gone"]))
        try:
            with monkeypatch.context() as patch:
                seen = _syncs(patch, root, dies_at="runs")
                with pytest.raises(_Died):
                    await _update(engine, tmp_path, _fw(paths["gone"], interval=61), "stable", "u1")
            return seen
        finally:
            await _crash(engine)

    assert asyncio.run(first()) == ["runs/w.1"]
    (_first, second), shapes = _attempts(tmp_path)
    assert shapes[1] == (2, 2, "runs/w.1/2") and not (root / "runs/w.1/2" / WATCH_LOG).exists()
    seen = _syncs(monkeypatch, root)
    launches = _launches(monkeypatch)

    async def restart() -> None:
        at = T0 + EVERY / 2
        engine = await _resume(tmp_path, at, "stable")
        try:
            await _until(engine, at)
        finally:
            await _stop(engine)

    asyncio.run(restart())
    assert [launch[:3] for launch in launches] == [("w", 1, second)]
    assert seen == ["runs/w.1", "runs", ".", "runs/w.1/2"]
    assert _lines(tmp_path, "runs/w.1/2") == ["start", "poll"]


def test_dl566_a_watch_s_forecast_reads_the_open_attempt_of_its_row_s_run_only() -> None:
    """`watch_ordinals` gives the forecast the ordinal of the attempt a stop of the live row
    selects. A watch's task cancelled when its next run started stays open until it is reaped,
    so an attempt of the earlier run can be open beside the current run's: it is not read. A
    command's attempts are not read either; its forecast is not the interruption rule's."""
    from dsl41.ir import lower_source
    from dsl41.reload_plan import Live
    from dsl41.runner_journal import OpenAttempt
    from dsl41.runner_update import watch_ordinals

    catalog = lower_source(
        "insert_job: w\njob_type: f\nwatch_file: /x\n\ninsert_job: c\njob_type: c\ncommand: true\n"
    )
    live = Live(catalog, {}, 1, {"w": "RUNNING", "c": "RUNNING"}, runs={"w": 2, "c": 1})

    def opened(job: str, run: int, ordinal: int, seq: int) -> OpenAttempt:
        return OpenAttempt(f"{job}{run}{ordinal}", job, run, ordinal, "spool", seq, "t")

    attempts = [opened("w", 1, 5, 10), opened("w", 2, 1, 11), opened("c", 1, 5, 12)]
    assert watch_ordinals(attempts, live) == {"w": 1}
    assert watch_ordinals(attempts[:1], live) == {}


@pytest.mark.parametrize("case", ["unchanged", "replacement"])
def test_dl566_a_run_root_reached_through_a_symlink_syncs_the_same_and_nothing_above_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """The root is written at its real path, then resumed by that path and, after a death,
    through `via`, a symlink to its parent: the store's spools keep the real path's name. Both
    restarts sync the same directories, addressed from the run root the engine was given, and
    open nothing above it; the watch runs after each. `unchanged` is attempt one in its run's
    own directory, the layout the build before writes; `replacement` is attempt two."""
    from dsl41 import runner_adapters
    from dsl41.runner_startup import resume_run
    from test_update_watches import _adapters

    paths = _files(tmp_path)
    (tmp_path / "via").symlink_to(tmp_path)

    async def first() -> None:
        engine = await _started(tmp_path, _fw(paths["gone"]))
        try:
            if case == "replacement":
                await _update(engine, tmp_path, _fw(paths["gone"], interval=61), "stable", "u1")
        finally:
            await _crash(engine)

    asyncio.run(first())
    synced: list[Path] = []
    real = runner_adapters.fsync_dir

    def recorded(path: Path) -> None:
        synced.append(path)
        real(path)

    monkeypatch.setattr(runner_adapters, "fsync_dir", recorded)

    async def restart(root: Path, at: datetime) -> list[str]:
        from dsl41.runner_clock import VirtualClock
        from dsl41.runner_config import EngineSettings

        del synced[:]
        engine = await resume_run(
            prepare_estate(tmp_path / "estate").catalog,
            root,
            clock=VirtualClock(start=at),
            adapters=_adapters("stable"),
            settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
        )
        try:
            await _until(engine, at)
            assert _status(engine) == "RUNNING" and "w" in engine.live_jobs()
        finally:
            await _crash(engine)
        # nothing above the run root the engine was given is opened
        assert all(path == root or root in path.parents for path in synced), synced
        return [str(path.relative_to(root)) for path in synced]

    by_name = asyncio.run(restart(tmp_path / "run", T0 + EVERY / 2))
    by_link = asyncio.run(restart(tmp_path / "via" / "run", T0 + EVERY))
    expected = {"unchanged": ["runs", "."], "replacement": ["runs/w.1", "runs", "."]}[case]
    assert by_name == by_link == expected
    spool = {"unchanged": "runs/w.1", "replacement": "runs/w.1/2"}[case]
    assert _attempts(tmp_path)[1][-1][2] == spool
    assert _lines(tmp_path, spool)[0] == "start" and _lines(tmp_path, spool).count("start") == 1
