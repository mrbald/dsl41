"""The engine's stops by the process group and the process cookie (`DSL41_COOKIE`): every process
of a command attempt carries `<root_id>.<run id>`, so where the cookie can be read (Linux,
with pidfd signalling) the engine knows its own processes whatever their group.

The owner's rule: what is left of an attempt (`EngineStops.left`) is the recorded group's live
members and the processes carrying its cookie, both taken as reliable; a scan that fails is
`unknown` and never releases. A process left when the command exits is a leak: the
`process-leak` switch stops it with the ordinary ladder (`terminate`) or leaves it running with
a warning (`leak`, the vendor's reading). The ladder signals each process by itself.

The units drive that function, the ladder, the held poll and the restart through stand-ins for
the scan, on any host. The scenarios run against real wrappers on the real clock, on Linux only.
Readiness comes from the child (a marker it writes once it is ready), never from a fixed sleep.
Assertions read the recorded order, never elapsed time.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from store_surgery import rows, update
from test_engine_stops import (
    GRACE,
    _finish,
    _jil,
    _kinds,
    _plans,
    _resume,
    _script,
    _spawned,
    _start,
    _start_jobs,
    _until,
)

from dsl41 import runner_procid, runner_startup, runner_stops
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter, LocalCommandAdapter
from dsl41.runner_attempts import missing_record_verdict
from dsl41.runner_clock import RealClock, VirtualClock
from dsl41.runner_config import EngineSettings, RuntimeProfile
from dsl41.runner_control import status_label
from dsl41.runner_journal import OpenAttempt
from dsl41.runner_startup import resume_run, start_run
from dsl41.engine_charts import AttemptOutcome
from dsl41.runner_stops import COMMAND_SURVIVED, NOTHING_LEFT, AttemptFacts, Left

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the process cookie is read on Linux only"
)

T0 = datetime(2026, 7, 1, 8, 0)
RUN_ID = "5b0c3f0e-6a52-4d3e-9a0b-2f7d6c1e4a10"
OTHER_ID = "0f6a8f4c-1c2d-4b5e-8f90-a1b2c3d4e5f6"
COOKIE = runner_procid.cookie("inst", RUN_ID)
PGID = 2**22 + 7
SPAWN = {
    "version": 1,
    "run_id": RUN_ID,
    "job": "j",
    "run_number": 1,
    "command_pgid": PGID,
    "command_pid": PGID,
    "command_start_time": "t",
    "wrapper_pid": 41,
    "wrapper_start_time": "ticks:41",
}
_JIL = "insert_job: j\njob_type: c\ncommand: sleep 30\n"


def _found(*pids: int, run_id: str = RUN_ID, error: int | None = None) -> runner_procid.Ours:
    return runner_procid.Ours({pid: (run_id, f"ticks:{pid}") for pid in pids}, 0, error)


def _engine(tmp_path: Path, kill_s: float = 0.1) -> Engine:
    return start_run(
        lower_source(_JIL),
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        settings=EngineSettings(grace_term_s=0.05, grace_kill_s=kill_s),
    )


def _close(engine: Engine) -> None:
    assert engine.journal is not None
    engine.journal.close()


class _World:
    """Stand-ins for what `left` reads (the scans in order, the leader's token check, the
    recorded group's members) and for the signals sent by pid, with the loop time of each."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.scans: list[runner_procid.Ours | None] = [_found()]
        self.leader = False
        self.group: dict[int, str] | None = {}
        self.sent: list[tuple[int, int]] = []
        self.killed_at: float | None = None
        monkeypatch.setattr(runner_procid, "ours", self._ours)
        monkeypatch.setattr(runner_procid, "verify_alive", lambda pid, _t: self._alive(pid))
        monkeypatch.setattr(runner_procid, "linux_group_pids", lambda _pgid: self.group)
        monkeypatch.setattr(runner_procid, "signal_ours", self._signal)

    def _ours(self, _prefix: str) -> runner_procid.Ours | None:
        return self.scans.pop(0) if len(self.scans) > 1 else self.scans[0]

    def _alive(self, pid: int) -> bool:
        return self.leader if pid == PGID else False

    def _signal(self, pid: int, _start: str, sig: int) -> bool:
        self.sent.append((pid, sig))
        if sig == signal.SIGKILL:
            self.killed_at = asyncio.get_running_loop().time()
        return True

    @property
    def killed(self) -> list[int]:
        return [pid for pid, sig in self.sent if sig == signal.SIGKILL]


def _adopt(
    engine: Engine, attempt: str = RUN_ID, cookie: str | None = COOKIE, proven: bool = False
) -> None:
    env = {} if cookie is None else {"DSL41_COOKIE": cookie}
    engine.adopt_attempt(AttemptFacts(attempt, "j", 1, 1, Path("/nonexistent"), env))
    engine.stops.scan_available = True
    engine.stops.note_boot({**SPAWN, "run_id": attempt})
    if proven:
        _prove(engine, attempt)


def _prove(engine: Engine, attempt: str = RUN_ID) -> None:
    """One reading while the recorded leader passes its start token: it proves the group, as
    the live engine's first reading at `spawn.json` does."""
    with pytest.MonkeyPatch.context() as leader:
        leader.setattr(runner_procid, "verify_alive", lambda *_: True)
        leader.setattr(runner_procid, "linux_group_pids", lambda pgid: {pgid: "t"})
        assert engine.stops.left(PGID, "t", attempt).alive


def _held(engine: Engine, tmp_path: Path, attempt: str, job: str) -> None:
    """A restart's closing attempt whose cleanup found its command outlived the KILL: restored
    ENDED and held, as the live survivor verdict holds one (spec-restart §5)."""
    env = {"DSL41_COOKIE": runner_procid.cookie("inst", attempt)}
    facts = AttemptFacts(attempt, job, 1, 1, tmp_path / attempt, env, exited=True, closing=True)
    engine.adopt_attempt(facts, AttemptOutcome("stopped"), plan_done=True)
    engine.stops.scan_available = True
    engine.stops.note_boot({**SPAWN, "run_id": attempt})
    engine.stops.hold(attempt, COMMAND_SURVIVED)
    engine.hold_group(attempt, PGID)


def _leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_stops.EngineStops, "leak_policy", property(lambda _s: "leak"))


# ------------------------------------------------------------------- the one function


def test_what_is_left_is_the_group_and_the_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What is left is the union of the recorded group's live members and the processes
    carrying the cookie, the wrapper left out. A reading proves a group the attempt's (its
    leader passes its token, a member carries the cookie) and the proof holds while the group
    has members; a group with members and no proof is never counted. A group recorded under
    another boot left nothing."""
    world = _World(monkeypatch)

    async def scenario() -> None:
        engine = _engine(tmp_path)
        stops = engine.stops
        _adopt(engine)
        world.scans = [_found(41, 42)]
        assert stops.left(PGID, "t") == Left("ours", {42: "ticks:42"}, "processes")
        world.leader, world.group = True, {PGID: "t", 50: "ticks:50"}
        assert set(stops.left(PGID, "t").pids) == {PGID, 50, 42}
        world.leader, world.scans, world.group = False, [_found()], {50: "ticks:50"}
        assert set(stops.left(PGID, "t").pids) == {50}  # proven while it has members
        world.group = {}
        assert stops.left(PGID, "t") is NOTHING_LEFT  # empty: the proof ends
        world.group = {50: "ticks:50"}
        assert stops.left(PGID, "t") is NOTHING_LEFT  # a stranger holds the group id
        world.scans = [_found(50)]
        assert set(stops.left(PGID, "t").pids) == {50}  # a member carries the cookie
        world.group = {}
        stops.left(PGID, "t")
        _prove(engine)
        world.scans, world.group = [_found()], {50: "ticks:50"}
        assert set(stops.left(PGID, "t").pids) == {50}  # a reading proved the group
        stops.note_cookie(OTHER_ID, {"DSL41_COOKIE": runner_procid.cookie("inst", OTHER_ID)})
        world.scans = [_found(41, run_id=OTHER_ID)]  # no spawn noted: no wrapper to leave out
        assert set(stops.left(PGID, "t", OTHER_ID).pids) == {41, 50}
        stops.note_boot({**SPAWN, "boot_id": "another boot"})
        assert stops.left(PGID, "t") is NOTHING_LEFT
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())


def test_a_failed_scan_is_unknown_and_still_names_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra B3: a scan cut short, a /proc that cannot be listed or a scan that could not be
    made is `unknown`: alive, never a release, its note saying why, and the group's members
    are still named for the ladder."""
    world = _World(monkeypatch)

    async def scenario() -> None:
        engine = _engine(tmp_path)
        _adopt(engine, proven=True)
        world.group = {50: "ticks:50"}
        world.scans = [_found(error=errno.EMFILE)]
        look = engine.stops.left(PGID, "t")
        assert look == Left("unknown", {50: "ticks:50"}, "scan failed: EMFILE")
        assert look.alive and engine.stops.command_alive(PGID, "t")
        world.scans, world.group = [_found()], None
        assert engine.stops.left(PGID, "t").reason == "scan failed: EIO"
        world.scans, world.group = [None], {}
        assert engine.stops.left(PGID, "t").kind == "unknown"
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())


def test_without_a_cookie_or_a_scan_the_group_checks_decide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback lives in the same function, as before the cookie: the leader's token, then
    a live member of a group a ladder signalled or a held attempt's."""
    world = _World(monkeypatch)
    live = [True]
    monkeypatch.setattr(runner_stops, "group_has_live_member", lambda _pgid: live[0])

    async def scenario() -> None:
        engine = _engine(tmp_path)
        stops = engine.stops
        _adopt(engine, cookie=None)
        assert stops.left(PGID, "t") is NOTHING_LEFT  # not laddered, not held
        stops.note_laddered(PGID)
        assert stops.left(PGID, "t") == Left("ours", reason=COMMAND_SURVIVED)
        live[0] = False
        assert stops.left(PGID, "t") is NOTHING_LEFT
        world.leader = True
        assert stops.left(PGID, "t") == Left("ours", reason="leader")
        stops.scan_available = False
        _adopt(engine, OTHER_ID)
        stops.scan_available = False
        assert stops.left(PGID, "t", OTHER_ID) == Left("ours", reason="leader")
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())


def test_a_live_leader_without_the_cookie_is_wrapper_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus B1: a leader whose environment shows no cookie and that passes its start-token
    check is left, so a wrapper lost beside it is `wrapper_lost` and the ladder runs."""
    world = _World(monkeypatch)

    async def scenario() -> None:
        engine = _engine(tmp_path)
        _adopt(engine)
        world.leader, world.group = True, {PGID: "t"}
        verdict = missing_record_verdict(
            SPAWN, wrapper_alive=False, command_alive=engine.stops.command_alive
        )
        assert verdict == "wrapper_lost"
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())


# ------------------------------------------------------------------- the leak


@pytest.mark.parametrize("policy", ["terminate", "leak"])
def test_a_failed_scan_with_a_live_group_child_is_stopped_and_never_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    """Astra B3, B2: the leader exits 0 while a child lives in its group and the scan fails.
    The attempt has leaked: under `terminate` the ladder TERMs and KILLs the group's member
    by pid; it survives, so the attempt is held, and no look while the scan fails releases
    it. The first look that finds nothing releases it in one call, and the attempt closes.
    Under `leak` nothing is ever signalled, but a failed reading releases nothing either: the
    attempt is held until a reading succeeds (Astra round 1, M3)."""
    world = _World(monkeypatch)
    if policy == "leak":
        _leak(monkeypatch)

    async def scenario() -> tuple[list[str], bool]:
        engine = _engine(tmp_path)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _adopt(engine, proven=True)
        world.group, world.scans = {50: "ticks:50"}, [_found(error=errno.EMFILE)]
        engine.attempts.spawn_recorded(RUN_ID, SPAWN)
        engine.attempts.status_recorded(RUN_ID, {"version": 1, "outcome": "exited", "exit_code": 0})
        await engine.attempts.join()
        notes = [str(engine.attempt_facts(RUN_ID).note)]
        held = RUN_ID in engine.held_groups
        if held:
            engine.stops._look()
            assert engine.blocked("j") == RUN_ID  # unknown never releases
            notes.append(str(engine.attempt_facts(RUN_ID).note))
            world.group, world.scans = {}, [_found()]
            engine.stops._look()
            assert RUN_ID not in engine.held_groups  # one look that finds nothing
        await engine.shutdown()
        _close(engine)
        return notes, held

    notes, held = asyncio.run(scenario())
    events = [b.get("event") for _, k, b in rows(tmp_path / "run") if k == "follow_up"]
    if policy == "terminate":
        assert held and world.sent[:2] == [(50, signal.SIGTERM), (50, signal.SIGKILL)]
        assert notes == ["command survived its KILL", "scan failed: EMFILE"]
        assert "ladder_done" in events
    else:
        assert held and world.sent == [] and notes == ["scan failed: EMFILE"] * 2
        assert "ladder_done" in events


def test_a_leak_is_noted_and_its_pids_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """The note and the warning name what leaked: `leaked: n, stopping` under `terminate`,
    `leaked: n, left running` under `leak`, with the pids."""
    world = _World(monkeypatch)

    async def scenario() -> list[str]:
        engine = _engine(tmp_path)
        _adopt(engine)
        engine.attempts.spawn_recorded(RUN_ID, SPAWN)
        world.scans = [_found(42, 43)]
        notes = []
        for leaks in (False, True):
            if leaks:
                _leak(monkeypatch)
            engine.stops.leaked(RUN_ID)
            notes.append(str(engine.attempt_facts(RUN_ID).note))
        await engine.shutdown()
        _close(engine)
        return notes

    assert asyncio.run(scenario()) == ["leaked: 2, stopping", "leaked: 2, left running"]
    assert "leaked: 2, left running (pids 42 43)" in capfd.readouterr().err


def test_under_leak_a_successful_reading_releases_a_failed_scan_hold_without_signalling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """A failed reading holds a leak without signalling. The first successful reading names
    its live processes and releases it (spec-restart §3, DL-433)."""
    world = _World(monkeypatch)
    _leak(monkeypatch)

    async def scenario() -> None:
        engine = _engine(tmp_path)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _adopt(engine)
        world.scans = [_found(error=errno.EMFILE)]
        engine.attempts.spawn_recorded(RUN_ID, SPAWN)
        engine.attempts.status_recorded(RUN_ID, {"version": 1, "outcome": "exited", "exit_code": 0})
        await engine.run_until_quiescent(T0)
        engine.stops._look()
        assert engine.blocked("j") == RUN_ID and world.sent == []
        assert engine.attempt_facts(RUN_ID).note == "scan failed: EMFILE"

        world.scans = [_found(43, 42)]
        engine.stops._look()
        assert engine.blocked("j") is None and engine.held_groups == {}
        assert world.sent == [] and engine.oracle.store.job["j"].status == "SUCCESS"
        engine.stops._look()
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    warning = "j.1.1: leaked: 2, left running (pids 42 43)"
    assert capfd.readouterr().err.count(warning) == 1
    events = [b.get("event") for _, k, b in rows(tmp_path / "run") if k == "follow_up"]
    assert events.count("ladder_done") == 1


# ------------------------------------------------------------------- the ladder and the poll


def test_the_record_timeout_is_armed_at_the_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra m1: the ladder ends at its KILL and the tier arms the record timeout there, so a
    grace shorter than any poll still runs from the KILL."""
    world = _World(monkeypatch)

    async def scenario() -> float:
        engine = _engine(tmp_path, kill_s=0.01)
        _adopt(engine)
        engine.attempts.spawn_recorded(RUN_ID, SPAWN)
        world.scans = [_found(42)]
        engine.attempts.stop(RUN_ID, "engine_stop")
        await engine.attempts.join()
        assert world.killed == [42] and world.killed_at is not None
        due = engine.stops._timers[RUN_ID].when() - world.killed_at
        await engine.shutdown()
        _close(engine)
        return due

    assert asyncio.run(scenario()) <= 0.01 + 0.005


def test_the_held_poll_kills_what_outlived_a_stop_and_releases_at_the_first_empty_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus m7, F-m2: each look KILLs the processes found of a held attempt that outlived a
    stop. A failed reading shows `scan failed: <errno>` only while readings fail; the next
    successful one restores the hold's own note. A look that finds nothing releases at once."""
    world = _World(monkeypatch)

    async def scenario() -> list[str | None]:
        engine = _engine(tmp_path)
        _held(engine, tmp_path, RUN_ID, "j")
        world.scans = [runner_procid.Ours({50: (RUN_ID, "ticks:50"), 60: (OTHER_ID, "ticks:60")})]
        engine.stops._look()
        assert world.killed == [50]
        notes = []
        for scan in (_found(50, error=errno.EMFILE), _found(50)):
            world.scans = [scan]
            engine.stops._look()
            notes.append(engine.attempt_facts(RUN_ID).note)
        world.scans = [_found()]
        engine.stops._look()
        assert not engine.held_groups
        await engine.shutdown()
        _close(engine)
        return notes

    assert asyncio.run(scenario()) == ["scan failed: EMFILE", COMMAND_SURVIVED]


def test_a_second_release_of_the_same_attempt_does_nothing(tmp_path: Path) -> None:
    """Opus n1: `group_gone` twice for one attempt releases it once."""

    async def scenario() -> None:
        engine = _engine(tmp_path)
        _held(engine, tmp_path, RUN_ID, "j")
        engine.group_gone(RUN_ID)
        engine.group_gone(RUN_ID)
        assert engine.blocked("j") is None
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    events = [b.get("event") for _, k, b in rows(tmp_path / "run") if k == "follow_up"]
    assert events == ["ladder_done"]


def test_a_forgotten_attempt_leaves_nothing_in_the_stops(tmp_path: Path) -> None:
    """R3: everything the stops keep of an attempt goes where the engine forgets it."""

    async def scenario() -> None:
        engine = _engine(tmp_path)
        stops = engine.stops
        _adopt(engine, proven=True)
        stops.note_laddered(PGID)
        stops.hold(RUN_ID, COMMAND_SURVIVED)
        stops.forget(RUN_ID)
        kept = (stops._cookies, stops._spawns, stops._runs, stops._boots, stops._laddered)
        assert all(not part for part in kept)
        assert not (stops._holds or stops._proven)
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())


# ------------------------------------------------------------------- the pidfd gate


@pytest.mark.parametrize("pidfd", [True, False], ids=["pidfd", "no-pidfd"])
def test_without_pidfd_the_engine_does_not_scan_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pidfd: bool
) -> None:
    """Astra M1, Opus M2: the scan decides only where its processes can be signalled through
    a pidfd; without one the engine keeps the group checks for its life and says so once at
    start (a log line, not a record)."""
    said: list[str] = []
    monkeypatch.setattr(runner_stops, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(runner_procid, "pidfd_supported", lambda: pidfd)
    monkeypatch.setattr(runner_stops, "best_effort_report", said.append)

    async def scenario() -> bool:
        engine = _engine(tmp_path)
        available = engine.stops.scan_available
        await engine.shutdown()
        _close(engine)
        return available

    assert asyncio.run(scenario()) is pidfd
    assert said == ([] if pidfd else ["dsl41: cookie scan unavailable: no pidfd"])


def test_a_restart_cannot_fail_for_want_of_pidfd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus M2: a restart's ladder whose process cannot be signalled (no pidfd) sends nothing,
    raises nothing, and holds the attempt."""
    me = os.getpid()
    token = runner_procid.proc_start_token(me)
    assert token is not None
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    found = runner_procid.Ours({me: (RUN_ID, token)})
    monkeypatch.setattr(runner_procid, "ours", lambda _prefix: found)

    async def scenario() -> list[str]:
        engine = _engine(tmp_path)
        engine.stops.scan_available = True
        closing = _closing_attempt(tmp_path, engine, {**SPAWN, "command_start_time": "ticks:1"})
        await _clean_up(engine, closing)
        held = list(engine.held_groups)
        await engine.shutdown()
        _close(engine)
        return held

    assert asyncio.run(scenario()) == [RUN_ID]


# ------------------------------------------------------------------- the restart


async def _clean_up(engine: Engine, closing: OpenAttempt) -> None:
    """The restart's cleanup of a closing attempt (spec-restart §5): restored ENDED with its
    committed outcome, one reading of what is left and the ladder on it, run to their end."""
    assert closing.spool is not None
    facts = AttemptFacts(
        closing.attempt,
        closing.job,
        closing.run_number,
        closing.ordinal,
        Path(closing.spool),
        dict(closing.env),
        exited=True,
        closing=True,
    )
    outcome = AttemptOutcome(cast(Any, closing.outcome or "stopped"))
    engine.adopt_attempt(facts, outcome, plan_done=True)
    runner_startup._cleanup(engine, closing, None)
    await engine.attempts.join()
    await asyncio.sleep(0)


def _closing_attempt(
    tmp_path: Path, engine: Engine, spawn: dict[str, Any], outcome: str = "natural"
) -> OpenAttempt:
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "spawn.json").write_text(json.dumps({**spawn, "boot_id": engine.stops.boot_id}))
    env = {"DSL41_COOKIE": COOKIE}
    return OpenAttempt(
        RUN_ID, "j", 1, 1, str(spool), 0, T0.isoformat(), env, closing=True, outcome=outcome
    )


@pytest.mark.parametrize(
    "look",
    [NOTHING_LEFT, Left("ours", {42: "ticks:42"}), Left("unknown", reason="scan failed: EIO")],
    ids=["none", "ours", "unknown"],
)
def test_a_restart_stops_what_it_finds_left_of_a_closing_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, look: Left
) -> None:
    """The restart's cleanup stops what it finds left as the live record path does
    (spec-restart §5): nothing left closes the attempt; a survivor of the KILL holds it as
    `command survived its KILL` (Opus n2), KILLed at each look; a failed scan, which never
    releases, holds it the same way."""
    world = _World(monkeypatch)
    world.scans = [
        _found()
        if look.kind == "none"
        else _found(42)
        if look.kind == "ours"
        else _found(error=errno.EIO)
    ]

    async def scenario() -> tuple[list[str], str | None, bool]:
        engine = _engine(tmp_path)
        engine.stops.scan_available = True
        await _clean_up(engine, _closing_attempt(tmp_path, engine, SPAWN, outcome="stopped"))
        held = list(engine.held_groups)
        note = engine.attempt_facts(RUN_ID).note if held else None
        rekill = RUN_ID in engine.stops._holds
        await engine.shutdown()
        _close(engine)
        return held, note, rekill

    held, note, rekill = asyncio.run(scenario())
    events = [b.get("event") for _, k, b in rows(tmp_path / "run") if k == "follow_up"]
    if look.kind == "none":
        assert (held, events) == ([], ["ladder_done"])
    else:
        assert held == [RUN_ID] and events == [] and rekill
        assert note == COMMAND_SURVIVED


@pytest.mark.parametrize("found", [True, False], ids=["leaked", "nothing"])
def test_a_restart_under_leak_names_what_is_left_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], found: bool
) -> None:
    """Under `process-leak=leak` the restart signals nothing: it names what is left in a
    warning and closes the attempt, as the vendor would leave it."""
    world = _World(monkeypatch)
    _leak(monkeypatch)
    world.scans = [_found(42) if found else _found()]

    async def scenario() -> None:
        engine = _engine(tmp_path)
        engine.stops.scan_available = True
        await _clean_up(engine, _closing_attempt(tmp_path, engine, SPAWN))
        assert not engine.held_groups
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    events = [b.get("event") for _, k, b in rows(tmp_path / "run") if k == "follow_up"]
    assert events == ["ladder_done"] and world.sent == []
    assert ("leaked: 1, left running (pids 42)" in capfd.readouterr().err) is found


def test_a_stranger_in_the_recorded_group_is_never_signalled_and_the_attempt_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stranger holds the recorded group id after a pid wrap (a stand-in listing): its
    leader fails the token and none of its members carries the cookie, so no reading proves
    the group. The restart signals nothing and closes the attempt."""
    world = _World(monkeypatch)
    world.group = {PGID: "ticks:99", 51: "ticks:51"}

    async def scenario() -> None:
        engine = _engine(tmp_path)
        engine.stops.scan_available = True
        await _clean_up(engine, _closing_attempt(tmp_path, engine, SPAWN))
        assert not engine.held_groups
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    events = [b.get("event") for _, k, b in rows(tmp_path / "run") if k == "follow_up"]
    assert events == ["ladder_done"] and world.sent == []


def test_the_restart_never_reads_the_previous_engine_s_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R3 (spec-restart §4, DL-433): the restart neither awaits nor watches a previous
    engine's wrapper. Its wrapper alive in this boot, an attempt with no record ends
    `engine_death` at its observation, and the wrapper's pid is never read."""
    read: list[int] = []

    def verify(pid: int, _token: str) -> bool:
        read.append(pid)
        return pid == 41  # the recorded wrapper lives; its command does not

    monkeypatch.setattr(runner_procid, "verify_alive", verify)
    monkeypatch.setattr(runner_stops.EngineStops, "scan", lambda _self: None)
    run_root, dispatch = _open_with_cookie(tmp_path)
    spool = Path(dispatch["spool"])
    spawn = {**SPAWN, "run_id": dispatch["attempt"], "boot_id": runner_procid.current_boot_id()}
    (spool / "spawn.json").write_text(json.dumps(spawn))

    async def scenario() -> None:
        engine = await resume_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
        )
        engine.stops.scan_available = False
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    [outcome] = [b for _, k, b in rows(run_root) if k == "outcome"]
    assert (outcome["outcome"], outcome["cause"]) == ("unobservable", "engine_death")
    assert 41 not in read


def _open_with_cookie(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """A store with one open command attempt whose dispatch holds a cookie."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _engine(tmp_path)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    [(seq, dispatch)] = [(q, b) for q, k, b in rows(run_root) if k == "dispatch"]
    dispatch = {**dispatch, "env": {**dispatch["env"], "DSL41_COOKIE": COOKIE}}
    update(run_root, seq, dispatch)
    Path(dispatch["spool"]).mkdir(parents=True)
    return run_root, dispatch


def test_a_restart_stops_the_leak_a_record_it_reads_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus m4, the crash before the outcome was journaled: the attempt ended by its own
    natural record, so the first reading leaves its process to the live rule; the observation
    reads the record and the leak ladder TERMs and KILLs it, as the live engine would; once
    nothing is left the attempt closes (spec-restart §3, §4)."""
    world = _World(monkeypatch)
    monkeypatch.setattr(runner_stops, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(runner_procid, "pidfd_supported", lambda: True)
    run_root, dispatch = _open_with_cookie(tmp_path)
    run_id, spool = dispatch["attempt"], Path(dispatch["spool"])
    spawn = {**SPAWN, "run_id": run_id, "boot_id": runner_procid.current_boot_id()}
    (spool / "spawn.json").write_text(json.dumps(spawn))
    status = {**{k: SPAWN[k] for k in ("version", "job", "run_number")}, "run_id": run_id}
    (spool / "status.json").write_text(json.dumps({**status, "outcome": "exited", "exit_code": 0}))
    # the first reading (which leaves a natural end to the live rule), the outcome's, the
    # leak's, the TERM's, the KILL's, then nothing
    world.scans = [_found(42, run_id=run_id)] * 5 + [_found(run_id=run_id)]

    async def scenario() -> None:
        engine = await resume_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
        )
        await engine.run_until_quiescent(T0)
        await engine.attempts.join()
        await asyncio.sleep(0)
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    events = [b.get("event") for _, k, b in rows(run_root) if k == "follow_up"]
    assert world.sent == [(42, signal.SIGTERM), (42, signal.SIGKILL)]
    assert "ladder_done" in events


# ------------------------------------------------------------------- round 3: the two rules


class _Procs:
    """A stand-in box of processes: each has a group, may carry the attempt's cookie, may
    ignore TERM. The scan, the group reader, the leader's token and the signals read and act
    on it, so a TERM or a KILL changes what the next reading finds."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, **procs: tuple[int, bool, bool]) -> None:
        #: pid -> (pgrp, carries the cookie, ignores TERM)
        self.procs = {int(pid[1:]): spec for pid, spec in procs.items()}
        self.sent: list[tuple[int, int]] = []
        self.group_failed = False
        monkeypatch.setattr(runner_procid, "ours", self._ours)
        monkeypatch.setattr(runner_procid, "linux_group_pids", self._group)
        #: the start token each pid shows; a pid not named shows `ticks:<pid>`
        self.tokens: dict[int, str] = {}
        #: called with each signal before it takes effect
        self.on_signal: Any = None
        monkeypatch.setattr(runner_procid, "verify_alive", self._verify)
        monkeypatch.setattr(runner_procid, "signal_ours", self._signal)

    def _verify(self, pid: int, token: str) -> bool:
        return pid in self.procs and self.tokens.get(pid, f"ticks:{pid}") == token

    def _ours(self, _prefix: str) -> runner_procid.Ours:
        cookied = {pid for pid, (_g, cookie, _i) in self.procs.items() if cookie}
        return runner_procid.Ours({pid: (RUN_ID, f"ticks:{pid}") for pid in cookied})

    def _group(self, pgid: int) -> dict[int, str] | None:
        if self.group_failed:
            return None
        return {pid: f"ticks:{pid}" for pid, (g, _c, _i) in self.procs.items() if g == pgid}

    def _signal(self, pid: int, _start: str, sig: int) -> bool:
        self.sent.append((pid, sig))
        if self.on_signal is not None:
            self.on_signal(pid, sig)
        if pid in self.procs and (sig == signal.SIGKILL or not self.procs[pid][2]):
            del self.procs[pid]
        return True


@pytest.mark.parametrize("path", ["restart", "live"])
def test_a_proven_group_stays_ours_after_its_last_cookie_process_exits_on_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Astra B5: the group is proven the attempt's by its cookie member 50; 50 exits on the
    TERM while 51, a member without the cookie, ignores it. The proof holds while the group
    has members, so the KILL's reading still names 51 and KILLs it; the attempt closes only
    once 51 is gone."""
    box = _Procs(monkeypatch, p50=(PGID, True, False), p51=(PGID, False, True))

    async def scenario() -> list[str]:
        engine = _engine(tmp_path)
        engine.stops.scan_available = True
        if path == "restart":
            closing = _closing_attempt(tmp_path, engine, SPAWN, outcome="stopped")
            await _clean_up(engine, closing)
        else:
            _adopt(engine)
            engine.attempts.spawn_recorded(RUN_ID, SPAWN)
            engine.attempts.stop(RUN_ID, "engine_stop")
            await engine.attempts.join()
        held = list(engine.held_groups)
        await engine.shutdown()
        _close(engine)
        return held

    assert asyncio.run(scenario()) == []
    assert box.sent == [(50, signal.SIGTERM), (51, signal.SIGTERM), (51, signal.SIGKILL)]
    assert box.procs == {}
    if path == "restart":
        events = [b.get("event") for _, k, b in rows(tmp_path / "run") if k == "follow_up"]
        assert events == ["ladder_done"]


def test_a_group_proof_ends_at_the_first_empty_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra B4: a detached cookie process 60 keeps the attempt open after its group reads
    empty. The pgid is then reused by strangers (a new leader token, a member, no cookie):
    `left` names 60 only, and neither the ladder nor the re-KILLing poll signals a stranger."""
    box = _Procs(monkeypatch, p60=(60, True, True))

    async def scenario() -> list[Left]:
        engine = _engine(tmp_path)
        _held(engine, tmp_path, RUN_ID, "j")
        _prove(engine)
        looks = [engine.stops.left(PGID, "t", RUN_ID)]  # the group reads empty
        box.procs |= {PGID: (PGID, False, True), 99: (PGID, False, True)}  # strangers
        looks.append(engine.stops.left(PGID, "t", RUN_ID))
        await engine.stops.ladder(PGID, "t", 0.0)
        engine.stops._look()
        await engine.shutdown()
        _close(engine)
        return looks

    looks = asyncio.run(scenario())
    assert [set(look.pids) for look in looks] == [{60}, {60}]
    assert {pid for pid, _sig in box.sent} == {60}


def test_a_late_wrapper_exit_with_the_old_record_never_proves_a_recycled_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra B4 (round 4), through the real Observer: the first reading at `spawn.json` proves
    the group by its leader; the leader exits and a reading finds the group empty, which ends
    the proof, while a detached cookie process 60 keeps the attempt open; strangers then take
    the pgid (a new leader token, a member, no cookie); the wrapper exits with no record and
    the old `spawn.json`. The record proves nothing: the stop it starts, and the held poll,
    signal only 60."""
    from dsl41.runner_stops import Observer

    box = _Procs(monkeypatch, p4194311=(PGID, True, False), p60=(60, True, True))
    box.tokens[PGID] = "t"

    async def scenario() -> list[Left]:
        engine = _engine(tmp_path)
        _adopt(engine)
        observer = Observer(engine.stops, RUN_ID)
        observer.spawn_recorded(SPAWN)
        looks = [engine.stops.left(PGID, "t", RUN_ID)]
        del box.procs[PGID], box.tokens[PGID]  # the leader exits
        looks.append(engine.stops.left(PGID, "t", RUN_ID))  # the group reads empty
        box.procs |= {PGID: (PGID, False, True), 99: (PGID, False, True)}  # strangers
        observer.wrapper_exited(SPAWN)
        await engine.attempts.join()
        looks.append(engine.stops.left(PGID, "t", RUN_ID))
        engine.stops._look()
        await engine.shutdown()
        _close(engine)
        return looks

    looks = asyncio.run(scenario())
    assert [set(look.pids) for look in looks] == [{PGID, 60}, {60}, set()]
    assert {pid for pid, _sig in box.sent} == {60}
    assert (60, signal.SIGKILL) in box.sent


@pytest.mark.parametrize(
    "case",
    ["fork-after-the-kill-reading", "failed-group-reading-at-the-kill", "record-mid-ladder"],
)
def test_a_stop_whose_record_lands_after_its_ladder_takes_the_survivor_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """F-B1: a stop is finished only by a reading that finds nothing left. The leader carries
    the cookie and ignores TERM; member 51 has no cookie and ignores TERM. When the record
    (`signaled 9`) lands after the ladder ended, a reading follows it: a child 77 the leader
    forked after the KILL's reading, or 51 when the group reading failed at the KILL, is the
    stop's survivor. The attempt is held, the next run blocked, the poll KILLs it, and the
    attempt settles only when a reading finds nothing. A record that lands while the ladder
    runs is unchanged: the ladder runs on, KILLs both, and nothing is held."""
    box = _Procs(monkeypatch, p4194311=(PGID, True, True), p51=(PGID, False, True))
    box.tokens[PGID] = "t"
    survivor = {"fork-after-the-kill-reading": 77, "failed-group-reading-at-the-kill": 51}

    def hook(pid: int, sig: int) -> None:
        if case == "fork-after-the-kill-reading" and (pid, sig) == (PGID, signal.SIGKILL):
            box.procs[77] = (PGID, True, True)
        if case == "failed-group-reading-at-the-kill":
            box.group_failed = (pid, sig) == (51, signal.SIGTERM)

    box.on_signal = hook
    record = {"version": 1, "outcome": "signaled", "signal": 9}

    async def scenario() -> list[object]:
        engine = _engine(tmp_path)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _adopt(engine)
        engine.attempts.spawn_recorded(RUN_ID, SPAWN)
        engine.attempts.stop(RUN_ID, "engine_stop")
        if case == "record-mid-ladder":
            await asyncio.sleep(0)  # the ladder has TERMed and waits out the grace
            engine.attempts.status_recorded(RUN_ID, record)
            await engine.attempts.join()
            seen: list[object] = [RUN_ID in engine.held_groups, engine.blocked("j")]
        else:
            await engine.attempts.join()
            engine.attempts.status_recorded(RUN_ID, record)
            seen = [engine.blocked("j")]  # the verdict runs: the next run waits
            await engine.attempts.join()
            seen += [RUN_ID in engine.held_groups, engine.blocked("j")]
            engine.stops._look()  # KILLs the survivor
            seen.append(RUN_ID in engine.held_groups)
            engine.stops._look()  # nothing left
            seen.append(RUN_ID in engine.held_groups)
        await engine.shutdown()
        _close(engine)
        return seen

    seen = asyncio.run(scenario())
    assert box.procs == {}
    if case == "record-mid-ladder":
        assert seen == [False, None]
        assert (51, signal.SIGKILL) in box.sent
    else:
        assert seen == [RUN_ID, True, RUN_ID, True, False]
        assert box.sent[-1] == (survivor[case], signal.SIGKILL)
    outcome = [b for _, k, b in rows(tmp_path / "run") if k == "outcome"][0]
    assert outcome["outcome"] == "stopped"


def test_a_stat_reading_that_fails_holds_the_attempt_until_a_reading_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra M4: an EIO on a member's stat makes the group reading failed, never empty, so
    `left` is `unknown` beside a live cookie-less member: the natural outcome leaks, the
    ladder's survivor holds the attempt, and only the next successful reading releases it."""
    box = _Procs(monkeypatch, p51=(PGID, False, True))
    box.group_failed = True

    async def scenario() -> list[bool]:
        engine = _engine(tmp_path)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _adopt(engine, proven=True)
        assert engine.stops.left(PGID, "t").kind == "unknown"
        engine.attempts.spawn_recorded(RUN_ID, SPAWN)
        engine.attempts.status_recorded(RUN_ID, {"version": 1, "outcome": "exited", "exit_code": 0})
        await engine.attempts.join()
        seen = [RUN_ID in engine.held_groups]
        engine.stops._look()
        seen.append(RUN_ID in engine.held_groups)
        box.group_failed = False
        engine.stops._look()  # the reading decides: 51 is ours and is KILLed
        seen.append(RUN_ID in engine.held_groups)
        engine.stops._look()  # the group is empty
        seen.append(RUN_ID in engine.held_groups)
        await engine.shutdown()
        _close(engine)
        return seen

    assert asyncio.run(scenario()) == [True, True, True, False]
    assert box.sent == [(51, signal.SIGKILL)]


def test_a_process_gone_within_the_grace_is_never_reported_survived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Opus r3-m1: an ended attempt's survivor verdict is taken `grace_kill_s` after the
    KILL, as a stopping attempt's record timeout takes it. A process still tearing down at
    the KILL and gone within the grace is not reported survived, and the attempt closes."""
    world = _World(monkeypatch)
    torn: list[int] = []

    def tearing_down(pid: int, _start: str, sig: int) -> bool:
        world.sent.append((pid, sig))
        return True

    monkeypatch.setattr(runner_procid, "signal_ours", tearing_down)

    async def scenario() -> list[str]:
        engine = _engine(tmp_path, kill_s=5.0)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _adopt(engine, proven=True)
        # the outcome's look, the leak's, the TERM's, the KILL's, the first look after the
        # KILL (still tearing down), then gone
        world.scans = [_found(42)] * 5 + [_found()]
        engine.attempts.spawn_recorded(RUN_ID, SPAWN)
        engine.attempts.status_recorded(RUN_ID, {"version": 1, "outcome": "exited", "exit_code": 0})
        await engine.attempts.join()
        torn.extend(pid for pid, sig in world.sent if sig == signal.SIGKILL)
        held = list(engine.held_groups)
        await engine.shutdown()
        _close(engine)
        return held

    assert asyncio.run(scenario()) == [] and torn == [42]
    assert COMMAND_SURVIVED not in capfd.readouterr().err


def test_under_leak_a_stop_the_crash_cut_short_is_still_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Opus r3-m2: `process-leak=leak` governs leaks only. A closing attempt whose outcome is
    a stop (here `stopped`) has a stop the crash cut short: the restart finishes it whatever
    the switch, and its TERM-ignoring member is KILLed."""
    box = _Procs(monkeypatch, p42=(42, True, True))
    _leak(monkeypatch)

    async def scenario() -> None:
        engine = _engine(tmp_path)
        engine.stops.scan_available = True
        closing = _closing_attempt(tmp_path, engine, SPAWN, outcome="stopped")
        await _clean_up(engine, closing)
        assert not engine.held_groups
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    assert box.sent == [(42, signal.SIGTERM), (42, signal.SIGKILL)]
    assert "left running" not in capfd.readouterr().err
    events = [b.get("event") for _, k, b in rows(tmp_path / "run") if k == "follow_up"]
    assert events == ["ladder_done"]


# ------------------------------------------------------------------- real wrappers (Linux)


async def _written(path: Path) -> int:
    """Readiness from the child: the pid it wrote once it was ready."""
    await _until(lambda: path.exists() and path.read_text().strip() != "")
    return int(path.read_text().split()[0])


def _live(pid: int) -> bool:
    """A process that is running, not a zombie its parent has not reaped."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[0] != b"Z"
    except OSError:
        return False


def _kill(*paths: Path) -> None:
    for path in paths:
        with contextlib.suppress(ProcessLookupError, ValueError, FileNotFoundError):
            os.kill(int(path.read_text().split()[0]), signal.SIGKILL)


#: a TERM-ignoring child in the leader's group that writes its pid once it ignores TERM
_STUBBORN = "/bin/sh -c \"trap '' TERM; echo \\$\\$ > {file}; exec sleep 30\" &\n"
#: the leader waits for the child's marker, then exits 0
_THEN_EXIT = "while [ ! -s {file} ]; do sleep 0.01; done\nexit 0\n"
#: the first run leaves the child; a later run of the job only exits 0
_FIRST_ONLY = "if [ -e {flag} ]; then exit 0; fi\ntouch {flag}\n"

_LONG = EngineSettings(grace_term_s=1.0, grace_kill_s=1.0)


def _ladder_done_before_second_run(run_root: Path) -> None:
    first = _kinds(run_root, "dispatch")[0]["attempt"]
    closed = next(
        seq
        for seq, k, b in rows(run_root)
        if k == "follow_up" and b.get("attempt") == first and b["event"] == "ladder_done"
    )
    second = next(seq for seq, k, b in rows(run_root) if k == "dispatch" and b["run_number"] == 2)
    assert closed < second


@linux_only
def test_a_leaked_child_is_termed_then_killed_and_the_next_run_waits(tmp_path: Path) -> None:
    """Acceptance 2: the leader exits 0 leaving a TERM-ignoring child in its group. The child
    has leaked: it gets TERM, then KILL after the grace; the outcome stays what the command
    returned (SUCCESS, fed at the outcome); `status` says `leaked: 1, stopping`; a STARTJOB
    decided meanwhile is `dispatch_pending`; the attempt closes with `ladder_done`, and only
    then does run 2 launch."""
    run_root, flag, childfile = tmp_path / "run", tmp_path / "flag", tmp_path / "child"
    body = _FIRST_ONLY.format(flag=flag) + _STUBBORN.format(file=childfile)
    text = _jil({"j": _script(tmp_path, "leaks", body + _THEN_EXIT.format(file=childfile))})

    async def scenario() -> tuple[str, int]:
        engine = _start(run_root, text, settings=_LONG)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        child = await _written(childfile)
        await _until(lambda: engine.oracle.store.job["j"].status == "SUCCESS")
        await _until(lambda: engine.blocked("j") is not None)
        row = {"status": "SUCCESS", "attempt": engine.attempt_view("j")}
        _start_jobs(engine, "j")
        await _until(lambda: bool(_kinds(run_root, "dispatch_pending")))
        await _spawned(run_root, "j", run=2)
        await _until(lambda: engine.oracle.store.job["j"].status == "SUCCESS")
        await engine.request_stop()
        await _finish(engine, loop)
        return status_label(row), child

    try:
        label, child = asyncio.run(scenario())
        assert not _live(child)
    finally:
        _kill(childfile)
    assert label == "SUCCESS (held: leaked: 1, stopping)"
    first = _kinds(run_root, "outcome")[0]
    assert (first["outcome"], first["ladder"]) == ("natural", "running")
    assert [p["plan"] for p in _plans(run_root)][:1] == ["complete"]
    _ladder_done_before_second_run(run_root)


@linux_only
def test_a_leaked_setsid_child_with_its_cookie_is_found_and_stopped(tmp_path: Path) -> None:
    """Acceptance 2: the leader exits 0 leaving a child that left the group with `setsid` and
    ignores TERM. It keeps the cookie, so it has leaked like any child: TERM, the grace, KILL,
    and the attempt closes."""
    run_root, childfile = tmp_path / "run", tmp_path / "child"
    escape = (
        "import os,signal,time;os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"open({str(childfile)!r},'w').write(str(os.getpid()));time.sleep(30)"
    )
    body = f'{sys.executable} -c "{escape}" &\n' + _THEN_EXIT.format(file=childfile)
    text = _jil({"j": _script(tmp_path, "escapes", body)})

    async def scenario() -> int:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        child = await _written(childfile)
        assert os.getpgid(child) == child
        await _until(lambda: "ladder_done" in [b["event"] for b in _kinds(run_root, "follow_up")])
        await engine.request_stop()
        await _finish(engine, loop)
        return child

    try:
        child = asyncio.run(scenario())
        assert not _live(child)
    finally:
        _kill(childfile)
    [ended] = _kinds(run_root, "outcome")
    assert (ended["outcome"], ended["ladder"]) == ("natural", "running")


_MAIN_THREAD_EXITS = """
import ctypes, os, sys, threading, time
def worker():
    while open("/proc/self/stat").read().rsplit(")", 1)[1].split()[0] != "Z":
        time.sleep(0.01)
    open(sys.argv[1], "w").write(str(os.getpid()))
    time.sleep(30)
threading.Thread(target=worker).start()
while len(sys.argv) > 2 and not os.path.exists(sys.argv[2]):  # the test's go, when it gives one
    time.sleep(0.01)
ctypes.CDLL(None).pthread_exit(None)
"""


@linux_only
def test_a_leaked_child_whose_main_thread_exited_is_stopped(tmp_path: Path) -> None:
    """Astra B1: a child in the leader's group whose main thread exited while a worker runs
    shows no cookie, but the group witness counts it: the engine's first reading at
    `spawn.json` proved the group while its leader lived, so the child has leaked, and the
    ladder stops it. The child's main thread exits only once the engine has seen the spawn
    (a go file), so the first reading comes while the leader waits."""
    run_root, childfile, program = tmp_path / "run", tmp_path / "child", tmp_path / "main.py"
    go = tmp_path / "go"
    program.write_text(_MAIN_THREAD_EXITS)
    body = f"{sys.executable} {program} {childfile} {go} &\n" + _THEN_EXIT.format(file=childfile)
    text = _jil({"j": _script(tmp_path, "threads", body)})

    async def scenario() -> int:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        run_id = str(engine.attempt_view("j")["run_id"])  # type: ignore[index]
        await _until(lambda: engine.attempts.group(run_id) is not None)  # spawn seen
        go.write_text("go")
        child = await _written(childfile)
        await _until(lambda: "ladder_done" in [b["event"] for b in _kinds(run_root, "follow_up")])
        await engine.request_stop()
        await _finish(engine, loop)
        return child

    try:
        child = asyncio.run(scenario())
        assert not _live(child)
    finally:
        _kill(childfile)


@linux_only
def test_a_lost_wrapper_beside_a_leader_whose_main_thread_exited_is_wrapper_lost(
    tmp_path: Path,
) -> None:
    """Astra B1, wrapper loss: the leader's main thread exits while a worker runs, and its
    wrapper is killed. The leader still passes its start-token check, so the verdict is
    `wrapper_lost` and the ladder stops it, never `wrapper_gone` beside it."""
    run_root, childfile, program = tmp_path / "run", tmp_path / "child", tmp_path / "main.py"
    program.write_text(_MAIN_THREAD_EXITS)
    text = _jil({"j": f"exec {sys.executable} {program} {childfile}"})

    async def scenario() -> None:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        leader = await _written(childfile)
        view = engine.attempt_view("j")
        assert view is not None
        launch = engine.attempt_facts(str(view["run_id"])).launch
        assert launch is not None and launch.proc is not None
        os.kill(launch.proc.pid, signal.SIGKILL)
        await _until(lambda: len(_kinds(run_root, "outcome")) >= 1)
        await _until(lambda: not _live(leader))
        await engine.request_stop()
        await _finish(engine, loop)

    try:
        asyncio.run(scenario())
    finally:
        _kill(childfile)
    first = _kinds(run_root, "outcome")[0]
    assert (first["outcome"], first["cause"]) == ("unobservable", "wrapper_lost")


@linux_only
def test_a_child_that_leaves_the_group_and_drops_its_cookie_is_beyond_the_engine(
    tmp_path: Path,
) -> None:
    """The one stated limit: a child execed with `env -i` that also leaves the group with
    `setsid` carries no cookie and is in no recorded group. The engine makes no claim about
    it: the attempt closes while the child still lives. The unit's cgroup is the stronger
    fence."""
    run_root, childfile = tmp_path / "run", tmp_path / "child"
    program = (
        f"import os,time;os.setsid();open({str(childfile)!r},'w').write(str(os.getpid()));"
        "time.sleep(30)"
    )
    body = f'env -i {sys.executable} -c "{program}" &\n' + _THEN_EXIT.format(file=childfile)
    text = _jil({"j": _script(tmp_path, "scrubs", body)})

    async def scenario() -> Engine:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _until(lambda: engine.oracle.store.job["j"].status == "SUCCESS")
        await _until(lambda: engine.blocked("j") is None)
        assert not engine.held_groups
        assert _live(await _written(childfile))  # alive, and nothing holds its job
        await engine.request_stop()
        await _finish(engine, loop)
        return engine

    try:
        engine = asyncio.run(scenario())
    finally:
        _kill(childfile)
    assert engine.leaving_record == "clean_shutdown"
    [ended] = _kinds(run_root, "outcome")
    assert ended["outcome"] == "natural" and "ladder" not in ended


@linux_only
def test_under_leak_a_leaked_child_runs_on_and_the_next_run_launches_at_once(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """`process-leak=leak`, the vendor's reading: the child left behind runs on, a warning
    names the job, the run and its pid, nothing is signalled, the attempt settles at once,
    and a STARTJOB launches run 2 with no `dispatch_pending`."""
    run_root, flag, childfile = tmp_path / "run", tmp_path / "flag", tmp_path / "child"
    body = _FIRST_ONLY.format(flag=flag) + _STUBBORN.format(file=childfile)
    text = _jil({"j": _script(tmp_path, "leaks", body + _THEN_EXIT.format(file=childfile))})

    async def scenario() -> int:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=RealClock(),
            adapters={"CMD": LocalCommandAdapter(grace_seconds=GRACE.grace_term_s)},
            hold_open=True,
            settings=GRACE,
            launched=RuntimeProfile(semantics={"process-leak": "leak"}),
        )
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        child = await _written(childfile)
        await _until(lambda: engine.oracle.store.job["j"].status == "SUCCESS")
        await _until(lambda: engine.blocked("j") is None)
        _start_jobs(engine, "j")
        await _spawned(run_root, "j", run=2)
        assert _live(child)
        await engine.request_stop()
        await _finish(engine, loop)
        return child

    try:
        child = asyncio.run(scenario())
        assert _live(child)  # the drain leaves it too
    finally:
        _kill(childfile)
    assert f"j.1.1: leaked: 1, left running (pids {child})" in capfd.readouterr().err
    assert not _kinds(run_root, "dispatch_pending")
    first = _kinds(run_root, "outcome")[0]
    assert first["outcome"] == "natural" and "ladder" not in first


@linux_only
@pytest.mark.parametrize("window", ["after-the-record", "before-the-record"])
def test_a_restart_stops_a_leaked_child_whatever_the_crash_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: str
) -> None:
    """Acceptance 2, a crash between the outcome and the ladder: the leader exits 0 leaving a
    TERM-ignoring child, and the engine dies either while its leak ladder waits out the grace
    (the outcome journaled closing) or before it saw the record. Either way the restart stops
    the child, closes the attempt (`ladder_done`) and completes the run."""
    from dsl41.runner_stops import Observer

    run_root, childfile = tmp_path / "run", tmp_path / "child"
    body = _STUBBORN.format(file=childfile) + _THEN_EXIT.format(file=childfile)
    text = _jil({"j": _script(tmp_path, "leaks", body)})
    status = run_root / "runs" / "j.1" / "1" / "status.json"
    patient = EngineSettings(grace_term_s=30.0, grace_kill_s=1.0)

    async def crash() -> None:
        engine = _start(run_root, text, settings=patient)
        asyncio.ensure_future(engine.run_until_quiescent(datetime.max))  # cancelled below
        with monkeypatch.context() as blind:
            if window == "before-the-record":
                blind.setattr(Observer, "status_recorded", lambda *_: None)
                blind.setattr(Observer, "wrapper_exited", lambda *_: None)
            _start_jobs(engine, "j")
            await _written(childfile)
            if window == "after-the-record":
                await _until(lambda: len(_kinds(run_root, "outcome")) == 1)
            else:
                await _until(status.exists)
                view = engine.attempt_view("j")
                assert view is not None
                launch = engine.attempt_facts(str(view["run_id"])).launch
                assert launch is not None and launch.proc is not None
                proc = launch.proc
                await _until(lambda: proc.returncode is not None)  # the wrapper has exited
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in others:
                task.cancel()
            await asyncio.gather(*others, return_exceptions=True)
        assert engine.journal is not None
        engine.journal.close()

    try:
        asyncio.run(crash())
        child = int(childfile.read_text())
        assert _live(child)
        assert len(_kinds(run_root, "outcome")) == (1 if window == "after-the-record" else 0)

        async def restart() -> None:
            engine = await _resume(run_root, text)
            loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
            await _until(engine.started.is_set)
            await _until(lambda: engine.blocked("j") is None)
            await engine.request_stop()
            await _finish(engine, loop)

        asyncio.run(restart())
        assert not _live(child)
    finally:
        _kill(childfile)
    [ended] = _kinds(run_root, "outcome")
    assert ended["outcome"] == "natural"
    assert [p["plan"] for p in _plans(run_root)] == ["complete"]
    assert "ladder_done" in [b["event"] for b in _kinds(run_root, "follow_up")]
    assert len(_kinds(run_root, "dispatch")) == 1


@linux_only
def test_a_drain_stops_a_setsid_child_by_its_cookie(tmp_path: Path) -> None:
    """A stop reaches a `setsid` child that keeps the cookie and ignores TERM: the drain's
    ladder signals it by pid, KILLs it after the grace, and the drain ends clean."""
    run_root, childfile = tmp_path / "run", tmp_path / "child"
    escape = (
        "import os,signal,time;os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"open({str(childfile)!r},'w').write(str(os.getpid()));time.sleep(30)"
    )
    body = f'{sys.executable} -c "{escape}" &\nwait\n'
    text = _jil({"j": _script(tmp_path, "escapes", body)})

    async def scenario() -> tuple[Engine, int]:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        child = await _written(childfile)
        await _until(lambda: engine.attempt_view("j") is not None)
        await engine.request_stop()
        await _finish(engine, loop)
        return engine, child

    try:
        engine, child = asyncio.run(scenario())
        assert not _live(child)
    finally:
        _kill(childfile)
    assert engine.leaving_record == "clean_shutdown"
    [ended] = _kinds(run_root, "outcome")
    assert (ended["outcome"], ended["cause"]) == ("stopped", "engine_stop")


@pytest.mark.parametrize("outcome", ["natural", "stopped"])
@pytest.mark.parametrize("policy", ["terminate", "leak"])
def test_an_unknown_cleanup_reading_keeps_the_live_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, policy: str
) -> None:
    """Astra round 1 M3 (spec-restart §5, §7): a closing attempt's cleanup reading fails. The
    attempt keeps its one outcome and plan and is held as live until a reading succeeds,
    under either `process-leak` value and for a natural or a stopped outcome; a leak under
    `leak` is never signalled, and the first good reading releases it and closes it."""
    world = _World(monkeypatch)
    world.scans = [_found(error=errno.EIO)]
    if policy == "leak":
        _leak(monkeypatch)

    async def scenario() -> tuple[list[str], list[str]]:
        engine = _engine(tmp_path)
        engine.stops.scan_available = True
        await _clean_up(engine, _closing_attempt(tmp_path, engine, SPAWN, outcome=outcome))
        held = list(engine.held_groups)
        engine.stops._look()  # still failing: still held
        assert list(engine.held_groups) == held
        world.scans = [_found()]
        engine.stops._look()  # a reading that succeeds and finds nothing
        released = list(engine.held_groups)
        await engine.shutdown()
        _close(engine)
        return held, released

    held, released = asyncio.run(scenario())
    assert held == [RUN_ID] and released == []
    events = [b.get("event") for _, k, b in rows(tmp_path / "run") if k == "follow_up"]
    assert events == ["ladder_done"]  # closed once, by the good reading
    if policy == "leak" and outcome == "natural":
        assert world.sent == []
