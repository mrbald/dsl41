"""An update that changes a waiting member's condition gives it one attempt, through the engine
(DL-564, BUG-116).

The application selects the members and writes them into its `config applied` record as
`woken`; the record's `segment` carries them to every replay, and a replay takes them from
there alone. The engine's time advance after the application is the input that makes the
attempt, on the path with nothing to stop (update.07) and on the path through STOPPING
(update.08). The oracle's own scenarios are in `test_update_condition_attempt.py`.

The scenarios run on the virtual clock with fake command tasks. Every store a test leaves is
compared with its offline replay (`transitions_check`).
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import store_surgery
from store_surgery import rows
from test_engine_stops_cookie import PGID, RUN_ID, _Procs
from test_update_one_transaction import Crash, _down, _feed, _plan, _restart
from test_update_settlement import SIGNALED, _adopt, _engine
from transitions_check import (  # noqa: F401 -- autouse
    rows_equal_their_replay,
    rows_equal_their_replay_once,
)

from dsl41 import reload_update
from dsl41.canon import canonical_bytes
from dsl41.reload_update import Admitted
from dsl41.runner import Engine
from dsl41.runner_control import _applied as _answered
from dsl41.runner_journal import STORE_NAME, oracle_rows, read_backfill, replay_store
from dsl41.runner_store import Record, SqliteStore
from dsl41.runner_update import UpdateRequest

#: the cause of the update's attempt; the update applied configuration rev 2
CAUSE = "configuration rev 2 changed the condition (DL-564)"


def _box(name: str) -> str:
    return f"insert_job: {name}\njob_type: b\n\n"


def _cmd(name: str, extra: str = "", command: str = "sleep 30") -> str:
    return f"insert_job: {name}\njob_type: c\ncommand: {command}\n{extra}\n"


#: `P` runs and its member `X` waits on `s(Y)`; the update removes the condition
OLD = _box("P") + _cmd("X", "box_name: P\ncondition: s(Y)\n") + _cmd("Y")
NEW = _box("P") + _cmd("X", "box_name: P\n") + _cmd("Y")


def _applied(run_root: Path) -> tuple[int, dict[str, Any]]:
    [found] = [
        (seq, body)
        for seq, kind, body in rows(run_root)
        if kind == "config" and body.get("event") == "applied" and "token" in body
    ]
    return found


async def _update(engine: Engine, tmp_path: Path, text: str, rid: str = "u") -> Any:
    token = _plan(engine, tmp_path, text)
    answer = engine.request_update(UpdateRequest(token, 1, engine.baseline_id, rid))
    await engine.run_until_quiescent(engine.clock.now())
    return answer.result()


def _row(engine: Engine, job: str) -> tuple[str, int]:
    row = engine.oracle.store.job[job]
    return row.status, row.run_number


def test_dl564_bug116_the_engine_starts_the_member_in_the_input_after_the_application(
    tmp_path: Path,
) -> None:
    """BUG-116 through the engine: the update with nothing to stop records `woken: ["X"]`, its
    time advance starts `X` under the new definition, and `P` completes once `X` succeeds. The
    segment on the wire carries the names; the offline replay and a restart give the live
    engine's rows."""

    async def scenario() -> bytes:
        engine = _engine(tmp_path, OLD)
        try:
            await _feed(engine, "STARTJOB", "P")
            assert _row(engine, "P") == ("RUNNING", 1) and _row(engine, "X") == ("INACTIVE", 0)
            answer = await _update(engine, tmp_path, NEW)
            assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == ()
            assert answer.applied.result()["woken"] == ["X"]
            assert _row(engine, "X") == ("RUNNING", 1)
            assert engine.oracle.store.job["X"].started_by == CAUSE
            await _feed(engine, "STATUS", "X", status="SUCCESS")
            assert _row(engine, "P") == ("SUCCESS", 1)
            return canonical_bytes(oracle_rows(engine.oracle))
        finally:
            await _down(engine)

    live = asyncio.run(scenario())
    run_root = tmp_path / "run"
    seq, body = _applied(run_root)
    assert (body["changed"], body["woken"]) == (["X"], ["X"])
    assert "woken" not in _answered(body)  # the update's answer on the wire does not show it
    later = [b for s, k, b in rows(run_root) if s > seq and k == "dispatch"]
    assert [(d["job"], d["run_number"], d["rev"]) for d in later] == [("X", 1, 2)]
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        backfill = read_backfill(store, since=0, upto=store.last_seq()).records
        replayed = replay_store(store, where="observer")
    segments = [r for r in backfill if r["rec"] == "segment"]
    assert [(r["rev"], r.get("woken")) for r in segments] == [(1, None), (2, ["X"])]
    assert canonical_bytes(oracle_rows(replayed.oracle)) == live

    async def restarted() -> bytes:
        engine = await _restart(tmp_path)
        try:
            return canonical_bytes(oracle_rows(engine.oracle))
        finally:
            await _down(engine)

    assert asyncio.run(restarted()) == live


def test_dl564_a_config_applied_record_without_the_field_replays_with_no_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log a build before DL-564 wrote: the same update, whose application selected no member
    and whose `config applied` record has no `woken` key. Its replay makes no attempt, though
    the two catalogs differ in `X`'s condition: nothing infers the names from them. The store
    of the build with the rule replays to the start, as its live engine made it."""

    async def scenario(directory: Path) -> tuple[str, int]:
        directory.mkdir()
        engine = _engine(directory, OLD)
        try:
            await _feed(engine, "STARTJOB", "P")
            await _update(engine, directory, NEW)
            return _row(engine, "X")
        finally:
            await _down(engine)

    def replayed_x(run_root: Path) -> tuple[str, int]:
        with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
            row = replay_store(store, where="observer").oracle.store.job["X"]
        return row.status, row.run_number

    assert asyncio.run(scenario(tmp_path / "now")) == ("RUNNING", 1)
    assert replayed_x(tmp_path / "now" / "run") == ("RUNNING", 1)
    with monkeypatch.context() as patch:
        patch.setattr(reload_update, "woken_members", lambda *_: ())
        assert asyncio.run(scenario(tmp_path / "before")) == ("INACTIVE", 0)
    run_root = tmp_path / "before" / "run"
    seq, body = _applied(run_root)
    assert body.pop("woken") == []
    store_surgery.update(run_root, seq, body)
    assert replayed_x(run_root) == ("INACTIVE", 0)


def test_dl564_an_update_that_stops_a_member_records_and_makes_the_attempt_at_its_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path through STOPPING: the update changes the running member `Z`'s command, so it
    stops `Z`, and removes the waiting member `X`'s condition. The application at the look
    that finds the stop finished records `woken: ["X"]`, and its time advance starts `X`."""
    _Procs(monkeypatch, p4194311=(PGID, True, False))
    member = "box_name: P\n"
    old = OLD + _cmd("Z", member)
    new = NEW + _cmd("Z", member, command="sleep 31")

    async def scenario() -> None:
        engine = _engine(tmp_path, old)
        try:
            await _feed(engine, "STARTJOB", "P")
            assert _row(engine, "Z") == ("RUNNING", 1) and _row(engine, "X") == ("INACTIVE", 0)
            _adopt(engine, "Z", RUN_ID, PGID)
            answer = await _update(engine, tmp_path, new)
            assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == ("Z",)
            assert engine.update.stopping and _row(engine, "X") == ("INACTIVE", 0)
            engine.attempts.status_recorded(RUN_ID, SIGNALED)
            await engine.attempts.join()
            await asyncio.sleep(0)  # DL-431: the queued ladder-done callback releases the stop
            await engine.run_until_quiescent(engine.clock.now())
            assert not engine.update.stopping
            assert _row(engine, "X") == ("RUNNING", 1)
            assert engine.oracle.store.job["X"].started_by == CAUSE
        finally:
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())
    seq, body = _applied(tmp_path / "run")
    after = [(k, b) for s, k, b in rows(tmp_path / "run") if s > seq]
    # the time advance, then `X`'s launch under its run and the applied rev, then `Z`'s rerun
    # once its kept outcome commits at `finish`
    assert [k for k, _ in after[:3]] == ["input", "decision", "dispatch"]
    dispatched = [
        (b["job"], b["run_number"], b["rev"], b["ordinal"]) for k, b in after if k == "dispatch"
    ]
    assert dispatched == [("X", 1, 2, 1), ("Z", 1, 2, 2)]
    assert (body["changed"], body["stopped"], body["woken"]) == (
        ["X", "Z"],
        [["Z", "rerun"]],
        ["X"],
    )


class _DiesAfterApplied:
    """The engine dies at the first store write after the `config applied` record commits,
    before that write commits: after the application, before its time advance."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.applied = False
        real = SqliteStore.append

        def append(store: SqliteStore, *records: Record, **kwargs: Any) -> Any:
            if self.applied:
                raise Crash("the engine died after config applied")
            result = real(store, *records, **kwargs)
            self.applied = any(
                r.kind == "config" and r.payload.get("event") == "applied" for r in records
            )
            return result

        monkeypatch.setattr(SqliteStore, "append", append)


#: the rows the comparison reads: everything but the times
def _rows_but_time(engine: Engine) -> dict[str, tuple[Any, ...]]:
    return {
        name: (row.status, row.run_number, row.started_by, row.on_hold, row.on_ice)
        for name, row in sorted(engine.oracle.store.job.items())
    }


@pytest.mark.parametrize("path", ["update.07", "update.08"])
def test_dl564_a_death_after_the_application_makes_the_attempt_at_the_first_input_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The engine dies after `config applied` commits and before its time advance does, on
    either apply row. The restart reads the debt from the segment and feeds no input of its
    own: `X` waits. The first input of any kind, here `Y`'s FORCE_STARTJOB, makes the attempt in
    its time half, once, with the DL-564 cause. The rows equal an uncut run's that took the same
    input, but for the times."""
    _Procs(monkeypatch, p4194311=(PGID, True, False))
    stops = path == "update.08"
    member = "box_name: P\n"
    old = OLD + (_cmd("Z", member) if stops else "")
    new = NEW + (_cmd("Z", member, command="sleep 31") if stops else "")

    async def scenario(directory: Path, cut: bool) -> dict[str, tuple[Any, ...]] | None:
        directory.mkdir()
        engine = _engine(directory, old)
        try:
            await _feed(engine, "STARTJOB", "P")
            if stops:
                _adopt(engine, "Z", RUN_ID, PGID)
            with monkeypatch.context() as patch:
                if cut and not stops:
                    _DiesAfterApplied(patch)
                    with pytest.raises(Crash):
                        await _update(engine, directory, new)
                    return None
                answer = await _update(engine, directory, new)
                assert isinstance(answer.decision, Admitted)
                assert answer.decision.stop_set == (("Z",) if stops else ())
                if stops:
                    if cut:
                        _DiesAfterApplied(patch)
                    engine.attempts.status_recorded(RUN_ID, SIGNALED)
                    await engine.attempts.join()
                    await asyncio.sleep(0)  # DL-431: the ladder-done callback releases the stop
                    if cut:
                        with pytest.raises(Crash):
                            await engine.run_until_quiescent(engine.clock.now())
                        return None
                    await engine.run_until_quiescent(engine.clock.now())
            assert _row(engine, "X") == ("RUNNING", 1)
            await _feed(engine, "FORCE_STARTJOB", "Y")
            return _rows_but_time(engine)
        finally:
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    uncut = asyncio.run(scenario(tmp_path / "uncut", cut=False))
    assert uncut is not None
    directory = tmp_path / "cut"
    assert asyncio.run(scenario(directory, cut=True)) is None
    seq, body = _applied(directory / "run")
    assert body["woken"] == ["X"]
    assert not [k for s, k, _ in rows(directory / "run") if s > seq]  # died before the advance

    async def restarted() -> dict[str, tuple[Any, ...]]:
        engine = await _restart(directory)
        try:
            await engine.run_until_quiescent(engine.clock.now())
            assert not [k for s, k, _ in rows(directory / "run") if s > seq and k == "input"]
            assert _row(engine, "X") == ("INACTIVE", 0)
            assert engine.oracle._woken == {"X": CAUSE}
            await _feed(engine, "FORCE_STARTJOB", "Y")
            starts = [
                e
                for e in engine.oracle.trace()
                if (e.job, e.transition) == ("X", "INACTIVE->STARTING")
            ]
            assert [e.cause for e in starts] == [CAUSE]
            assert engine.oracle._woken == {}
            return _rows_but_time(engine)
        finally:
            await _down(engine)

    assert asyncio.run(restarted()) == uncut
