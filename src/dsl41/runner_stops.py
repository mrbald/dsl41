"""The engine's side of the attempt and engine lifecycle tiers (spec-wiring 2b, DL-426).

`runner_attempts` holds the two tiers and asks the world for what they need through two
protocols, `Processes` and `EngineEffects`. `EngineStops` is the engine's implementation of
both: it runs the stop ladder on a recorded group, arms the tiers' timers on the event loop,
reads an attempt's spool, and hands each outcome and each lifecycle intent back to the engine,
which journals it and carries out its plan. It looks for the groups of the held attempts, whose
jobs start nothing until they are gone. `Observer` turns one attempt's adapter reports into the
attempt tier's observations.

What is left of an attempt's command is one function, `EngineStops.left`, and every caller
reads it: the tier's `command_alive`, the held poll, the restart. Where the process cookie can
be read (`runner_procid.ours` on Linux, with pidfd signalling), what is left is the union of the
recorded process group's live members and the processes carrying the attempt's
`DSL41_COOKIE`, whatever their group, the wrapper left out. An attempt with no recorded group
(`runner_attempts.NO_GROUP`) is its cookie's processes alone (spec-restart §4). The cookie names
the run root, so one scan reads every attempt of the root, whichever engine start launched it
(spec-restart §3, DL-433). Both are taken as reliable: the
kernel keeps a group exactly, and a process that leaves the group and drops the cookie did so on
purpose and is beyond the engine by design. A reading that fails is `unknown`: its group members
are still signalled, and nothing is released on it. A group is proven the attempt's only by a
reading (its leader passes its start token, a member carries the cookie; the live engine reads
it as soon as it sees `spawn.json`); the proof holds while the group has members, cookie or
not, and ends at the first reading that finds it empty. A record never proves a group. A group with members and no proof is a stranger's that took
the id after a pid wrap: never counted, never signalled. The ladder signals each process by
itself (`stop_ladder` with `members`).

A process left when the command exits is a leak (the `process-leak` switch): `terminate`, the
default, runs the ladder on it; `leak`, the vendor's reading, leaves it running with a warning.

Elsewhere (macOS, no pidfd, an attempt dispatched before the cookie), the leader's start token
and the group decide as they did before the cookie, inside the same function, and the ladder
runs on the group.

The timers run on the process's monotonic clock, as the ladder does: the drain's deadline and
an attempt's `record_timeout` bound real processes, whatever clock domain the engine reads.
A timer that fires posts through the tier at once; the loop's next iteration carries out what
it implied (DL-426).
"""

from __future__ import annotations

import asyncio
import errno
import signal
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from dsl41 import runner_procid as _procid
from dsl41.engine_charts import AttemptOutcome
from dsl41.runner_adapters import Launch, group_has_live_member, spool_record, stop_ladder
from dsl41.runner_attempts import NO_GROUP, command_group
from dsl41.runner_journal import best_effort_report

if TYPE_CHECKING:  # annotation only
    from dsl41.runner import Engine

#: The note an attempt carries while its command outlived the KILL (DL-426).
COMMAND_SURVIVED: Final = "command survived its KILL"
#: The note of an attempt whose stop ladder keeps a later run of its job back (DL-426).
LADDER_RUNNING: Final = "stop ladder running"
#: The note of an unsettled attempt with no ladder running that keeps a later run back.
STOP_IN_PROGRESS: Final = "stop in progress"
#: How often a held attempt's group is looked for, in seconds (DL-426).
GROUP_POLL_S: Final = 0.5
#: How often what a KILL left is looked for, up to `grace_kill_s` after it, in seconds.
SETTLE_POLL_S: Final = 0.05


@dataclass
class AttemptFacts:
    """What the engine knows of one observed attempt: the run it belongs to, its ordinal, its
    spool and invocation, the cause of the stop it replaced (`after`), the adapter's hold on its
    wrapper, and the note `query` shows beside its state (DL-426)."""

    run_id: str
    job: str
    run_number: int
    ordinal: int
    spool: Path
    env: dict[str, str] = field(default_factory=dict)
    after: str | None = None
    launch: Launch | None = None
    note: str | None = None
    #: the wrapper has exited (or belonged to an earlier engine), and the plan is done
    exited: bool = False
    finished: bool = False
    #: its outcome was committed while its ladder ran: the ladder's end is recorded too
    closing: bool = False


class Observer:
    """One attempt's adapter reports, posted to the attempt tier under its run id."""

    def __init__(self, stops: EngineStops, run_id: str) -> None:
        self.stops = stops
        self.run_id = run_id

    def spawn_recorded(self, spawn: Mapping[str, Any]) -> None:
        self.stops.note_boot(spawn)
        self.stops.first_reading(spawn)
        self.stops.engine.attempts.spawn_recorded(self.run_id, spawn)

    def status_recorded(self, status: Mapping[str, Any]) -> None:
        self.stops.engine.attempts.status_recorded(self.run_id, status)

    def spawn_failed(self, error: str) -> None:
        self.stops.engine.attempts.spawn_failed(self.run_id, error)

    def wrapper_exited(self, spawn: Mapping[str, Any] | None) -> None:
        if spawn is not None:
            self.stops.note_boot(spawn)
        self.stops.engine.attempts.wrapper_exited(self.run_id, spawn)


@dataclass(frozen=True)
class Left:
    """What is left of an attempt's command (`EngineStops.left`). `ours`: something is left;
    `pids`, by pid with start tokens, are its processes where the cookie can be read (each may
    be signalled by itself), empty where only the group and the leader's token can tell.
    `unknown`: the scan failed (`reason`), so nothing is released; `pids` are the group's
    members, still signalled. `none`: nothing is left."""

    kind: Literal["ours", "unknown", "none"]
    pids: Mapping[int, str] = field(default_factory=dict)
    reason: str = ""

    @property
    def alive(self) -> bool:
        return self.kind != "none"


NOTHING_LEFT: Final = Left("none")


def _pids(pids: Mapping[int, str]) -> str:
    return " ".join(str(pid) for pid in sorted(pids)) or "?"


#: a shared scan not yet made: `left` scans for itself
_UNSCANNED: Final = object()


class EngineStops:
    """`Processes` and `EngineEffects` for one engine (module docstring)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.boot_id = _procid.current_boot_id()
        #: the cookie scan decides only where its processes can also be signalled (a pidfd)
        self.scan_available = sys.platform.startswith("linux") and _procid.pidfd_supported()
        if sys.platform.startswith("linux") and not self.scan_available:
            best_effort_report("dsl41: cookie scan unavailable: no pidfd")
        #: by attempt: its cookie and its spawn record; by recorded group: its boot and attempt
        self._cookies: dict[str, str] = {}
        self._spawns: dict[str, Mapping[str, Any]] = {}
        self._boots: dict[tuple[int, str], object] = {}
        self._runs: dict[tuple[int, str], str] = {}
        #: the recorded groups a reading proved the attempt's: every member is ours until a
        #: reading finds the group empty. `left` alone adds to it
        self._proven: set[tuple[int, str]] = set()
        self._timers: dict[str, asyncio.TimerHandle] = {}
        #: the groups a ladder has signalled: one of them still existing outlived its stop
        self._laddered: set[int] = set()
        #: each held attempt's own note, which a failed reading's note only covers while it lasts
        self._holds: dict[str, str] = {}
        #: the attempts whose processes a restart's first reading TERMed: their ladder's first
        #: rung is that TERM (spec-restart §3)
        self.termed: set[str] = set()
        #: the attempts held under `process-leak=leak` only while a reading fails: released at
        #: the first reading that succeeds, nothing signalled (`leaked`)
        self._leak_holds: set[str] = set()
        #: the poll that looks for the held attempts' groups, while any is held
        self._poll: asyncio.TimerHandle | None = None

    @property
    def leak_policy(self) -> str:
        """The `process-leak` switch the engine runs: `terminate` or `leak`."""
        return self.engine.oracle.semantics.process_leak

    # -- what the engine knows of an attempt's processes

    def note_boot(self, spawn: Mapping[str, Any]) -> None:
        """Keep a `spawn.json`: under its run id, and its boot and run under the group it
        names. A record proves nothing about the group now: only `left`'s readings do."""
        pgid, token = spawn.get("command_pgid"), spawn.get("command_start_time")
        run_id = spawn.get("run_id")
        if isinstance(run_id, str):
            self._spawns[run_id] = spawn
        if isinstance(pgid, int) and isinstance(token, str):
            self._boots[(pgid, token)] = spawn.get("boot_id", self.boot_id)
            if isinstance(run_id, str):
                self._runs[(pgid, token)] = run_id

    def first_reading(self, spawn: Mapping[str, Any]) -> None:
        """Read the group a `spawn.json` names as soon as the live engine sees it: its leader
        passes its start token then, so the reading proves the group (`left`) and the proof
        holds while the group has members, a cookie-less one whose leader later dies
        included. A leader that exits before this reading (the spool is polled every
        `SPOOL_POLL_S`) leaves its group unproven, as a restart finds one."""
        group = command_group(spawn)
        # a group `note_boot` kept, of an attempt where the cookie decides; the leader's token
        # proves it, so no cookie scan is made, only the group listing
        if group is not None and self._ladder_members(*group) is not None:
            self.left(*group, scan=_procid.Ours({}))

    def note_cookie(self, attempt: str, env: Mapping[str, str]) -> None:
        """Keep the cookie the attempt's `dispatch` record holds, if it holds one."""
        cookie = env.get(_procid.COOKIE_ENV)
        if cookie is not None:
            self._cookies[attempt] = cookie

    def note_laddered(self, pgid: int) -> None:
        """A ladder signalled this group: a member of it still alive outlived a stop."""
        self._laddered.add(pgid)

    def forget(self, attempt: str) -> None:
        """Drop everything kept of an attempt the engine forgets, in one place."""
        self._cookies.pop(attempt, None)
        self._holds.pop(attempt, None)
        self.termed.discard(attempt)
        self._leak_holds.discard(attempt)
        for group in [g for g, run in self._runs.items() if run == attempt]:
            del self._runs[group]
            self._boots.pop(group, None)
            self._proven.discard(group)
            self._laddered.discard(group[0])
        self._spawns.pop(attempt, None)

    def scan(self) -> _procid.Ours | None:
        """One scan of every process of this uid carrying a cookie of this run root, None where
        the scan cannot decide (spec-restart §3)."""
        return _procid.ours(f"{self.engine.root_id}.") if self.scan_available else None

    def left(
        self, pgid: int, start_token: str, attempt: str | None = None, scan: object = _UNSCANNED
    ) -> Left:
        """What is left of the command recorded as group `pgid` with leader token
        `start_token`, the attempt's run id found from the group when not given (module
        docstring). A group recorded under another boot left nothing (DL-42 item 5). `scan` is
        a shared scan of every cookie (the held poll's, the restart's first reading). `NO_GROUP`
        reads the cookie's processes alone, the attempt named by `start_token` when not given."""
        if self._boots.get((pgid, start_token), self.boot_id) != self.boot_id:
            return NOTHING_LEFT
        if pgid == NO_GROUP:
            attempt = attempt or start_token
        attempt = attempt or self._runs.get((pgid, start_token))
        cookie = None if attempt is None else self._cookies.get(attempt)
        if pgid == NO_GROUP and (cookie is None or not self.scan_available):
            return NOTHING_LEFT  # nothing names a process of it
        leader = pgid != NO_GROUP and _procid.verify_alive(pgid, start_token)
        if cookie is None or not self.scan_available:
            # the checks from before the cookie: the leader's token, then a live member of a
            # group a ladder signalled or a held attempt's
            if leader:
                return Left("ours", reason="leader")
            watched = pgid in self._laddered or attempt in self.engine.held_groups
            if watched and group_has_live_member(pgid):
                return Left("ours", reason=COMMAND_SURVIVED)
            return NOTHING_LEFT
        found = _procid.ours(cookie) if scan is _UNSCANNED else scan
        if not isinstance(found, _procid.Ours):  # a scan that could not be made at all
            found = _procid.Ours({}, error=errno.EIO)
        group = {} if pgid == NO_GROUP else _procid.linux_group_pids(pgid)
        wrapper = self._spawns.get(attempt or "", {}).get("wrapper_pid")
        mine = {pid: start for pid, (run, start) in found.pids.items() if run == attempt}
        members = group or {}
        key = (pgid, start_token)
        if group is not None and not members:
            self._proven.discard(key)  # the group is empty: its proof ends here
        elif leader or members.keys() & mine.keys():
            self._proven.add(key)  # proven now: its members stay ours while it has any
        pids = {**(members if key in self._proven else {}), **mine}
        if isinstance(wrapper, int):
            pids.pop(wrapper, None)
        if group is None or found.error is not None:
            failed = errno.errorcode.get(found.error or errno.EIO, "EIO")
            return Left("unknown", pids, f"scan failed: {failed}")
        return Left("ours", pids, "processes") if pids else NOTHING_LEFT

    def _members(self, pgid: int, start_token: str, attempt: str) -> Mapping[int, str]:
        return self.left(pgid, start_token, attempt).pids

    def _attempt_of(self, pgid: int, start_token: str) -> str | None:
        return start_token if pgid == NO_GROUP else self._runs.get((pgid, start_token))

    def _ladder_members(self, pgid: int, start_token: str) -> Any:
        """The ladder's `members` for the attempt's group where the cookie decides, else None
        (the group ladder)."""
        attempt = self._attempt_of(pgid, start_token)
        if attempt is None or attempt not in self._cookies or not self.scan_available:
            return None
        return partial(self._members, pgid, start_token, attempt)

    def command_alive(self, pgid: int, start_token: str) -> bool:
        """Whether anything of the recorded command is left (`left`)."""
        return self.left(pgid, start_token).alive

    def unnamed_alive(self, attempt: str) -> bool:
        """Whether a process carrying the cookie of an attempt with no recorded group is left
        (`left` under `NO_GROUP`, spec-restart §4)."""
        return self.left(NO_GROUP, attempt, attempt).alive

    async def outlives(self, pgid: int, start_token: str, grace_kill_s: float) -> bool:
        """Whether anything of the command is left `grace_kill_s` after the ladder's KILL,
        looked at every `SETTLE_POLL_S`: a process still tearing down at the KILL is given the
        grace the record timeout gives a stopping attempt."""
        deadline = time.monotonic() + grace_kill_s
        while self.command_alive(pgid, start_token):
            if time.monotonic() >= deadline:
                return True
            await asyncio.sleep(SETTLE_POLL_S)
        return False

    # -- Processes

    async def ladder(self, pgid: int, start_token: str, grace_term_s: float) -> None:
        """The ladder, by process where the cookie decides, else on the group; it ends at its
        KILL, where the tier arms the record timeout. An attempt whose processes the restart's
        first reading TERMed starts at the grace: that TERM was its first rung (spec-restart
        §3)."""
        self.note_laddered(pgid)
        members = self._ladder_members(pgid, start_token)
        attempt = self._attempt_of(pgid, start_token)
        if attempt in self.termed:
            self.termed.discard(attempt)
            await stop_ladder(pgid, start_token, grace_term_s, members=members, term=False)
        else:
            await stop_ladder(pgid, start_token, grace_term_s, members=members)

    def leaked(self, attempt: str) -> bool:
        """The attempt's command ended with no stop while processes of it are left: under
        `process-leak=terminate`, its note says so and the tier runs the ladder on them (True);
        under `leak`, they are left running with a warning naming them (False). Under `leak` a
        reading that failed releases nothing: the attempt is held, signalled never, until a
        reading succeeds, which then leaves what it finds with the warning (DL-431)."""
        facts = self.engine.attempt_facts(attempt)
        pgid, token = self.engine.attempts.cleanup_group(attempt)
        look = self.left(pgid, token, attempt)
        pids = " ".join(str(pid) for pid in sorted(look.pids)) or "?"
        terminate = self.leak_policy == "terminate"
        if look.kind == "unknown" and not terminate:
            self.engine.attempts.hold_unread(attempt)
            self._leak_holds.add(attempt)
            self._stuck(attempt, look.reason)
            return False
        facts.note = f"leaked: {len(look.pids)}, {'stopping' if terminate else 'left running'}"
        if look.kind == "unknown":
            facts.note = look.reason
        best_effort_report(
            f"dsl41: {facts.job}.{facts.run_number}.{facts.ordinal}: {facts.note} (pids {pids})"
        )
        return terminate

    def kill_wrapper(self, attempt: str) -> None:
        launch = self.engine.attempt_facts(attempt).launch
        if launch is not None:  # a restart's attempt has no wrapper of this engine's
            launch.abort()

    def arm_timer(self, key: str, generation: int, delay_s: float) -> None:
        self.cancel_timer(key)
        self._timers[key] = asyncio.get_running_loop().call_later(
            delay_s, self._record_timeout, key, generation
        )

    def cancel_timer(self, key: str) -> None:
        handle = self._timers.pop(key, None)
        if handle is not None:
            handle.cancel()

    def _record_timeout(self, key: str, generation: int) -> None:
        """The timer fired, outside any task: what it raises (a spool record that names another
        run, DL-118) stops the engine at the loop's next turn instead of vanishing into the event
        loop's handler."""
        self._timers.pop(key, None)
        try:
            self.engine.attempts.record_timeout(key, generation)
        except Exception as exc:  # noqa: BLE001 -- handed to the loop, which raises it
            self.engine.fail(exc)
        self.engine.wake()

    def read_spool(self, attempt: str) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
        spool = self.engine.attempt_facts(attempt).spool
        spawn = spool_record(spool / "spawn.json", attempt)
        if spawn is not None:
            self.note_boot(spawn)
        return spawn, spool_record(spool / "status.json", attempt)

    def record_outcome(self, attempt: str, outcome: AttemptOutcome) -> None:
        self.engine.record_outcome(attempt, outcome)

    def ladder_failed(self, attempt: str, error: BaseException) -> None:
        self._stuck(attempt, f"ladder failed: {error}")

    def command_survived(self, attempt: str) -> None:
        self._stuck(attempt, COMMAND_SURVIVED)

    def _stuck(self, attempt: str, note: str) -> None:
        """A stop that could not be carried out: the attempt stays unsettled, its job starts
        nothing until nothing of it is left, and the log and `query` say why (DL-426)."""
        facts = self.engine.attempt_facts(attempt)
        pgid, _ = self.engine.attempts.cleanup_group(attempt)
        facts.note = note
        self.hold(attempt, note)
        self.engine.hold_group(attempt, pgid)
        best_effort_report(f"dsl41: {facts.job}.{facts.run_number}.{facts.ordinal}: {note}")

    def hold(self, attempt: str, note: str) -> None:
        """A held attempt outlived a stop, and `note` is its own note, shown again once a
        failed reading has passed; the held poll KILLs what it finds of every held attempt."""
        self._holds[attempt] = note

    # -- the held groups

    def watch_groups(self) -> None:
        """Look for the held attempts' processes every `GROUP_POLL_S`, while any is held."""
        if self._poll is None:
            self._poll = asyncio.get_running_loop().call_later(GROUP_POLL_S, self._look)

    def _look(self) -> None:
        """One scan for every held attempt (`left`): an attempt with nothing left is released;
        what is found of one that outlived a stop is KILLed; one whose reading failed keeps its
        hold, and its note says why while the readings fail, its own note once one succeeds. A
        leak held under `process-leak=leak` while its reading failed is never signalled; the
        first reading that succeeds releases it, naming what it found (`leaked`).
        What that raises stops the engine at the loop's next turn (`_record_timeout`)."""
        self._poll = None
        try:
            scan = self.scan()
            for attempt, pgid in list(self.engine.held_groups.items()):
                token = self._spawns.get(attempt, {}).get("command_start_time")
                token = attempt if pgid == NO_GROUP else token
                look = self.left(pgid, token if isinstance(token, str) else "", attempt, scan)
                if attempt in self._leak_holds and look.kind == "ours":
                    facts = self.engine.attempt_facts(attempt)
                    best_effort_report(
                        f"dsl41: {facts.job}.{facts.run_number}.{facts.ordinal}: leaked:"
                        f" {len(look.pids)}, left running (pids {_pids(look.pids)})"
                    )
                    look = NOTHING_LEFT  # a leak's reading that succeeds releases it
                if look.kind == "none":
                    self._holds.pop(attempt, None)
                    self.engine.group_gone(attempt)
                    continue
                facts = self.engine.attempt_facts(attempt)
                own = self._holds.get(attempt, facts.note)
                facts.note = look.reason if look.kind == "unknown" else own
                if attempt in self._leak_holds:
                    continue  # a leak is never signalled; it waits for a reading
                for pid, start in look.pids.items():  # every other held one outlived a stop
                    _procid.signal_ours(pid, start, signal.SIGKILL)
        except Exception as exc:  # noqa: BLE001 -- handed to the loop, which raises it
            self.engine.fail(exc)
            return
        if self.engine.held_groups:
            self.watch_groups()
        self.engine.wake()

    # -- EngineEffects

    def journal_draining(self) -> None:
        self.engine.journal_lifecycle("draining")

    def cancel_watchers(self) -> None:
        self.engine.cancel_unobserved()

    def arm_abort_deadline(self, deadline: float) -> None:
        self.cancel_timer("")
        self._timers[""] = asyncio.get_running_loop().call_later(
            max(0.0, deadline - time.monotonic()), self.engine.post_control, "abort_deadline"
        )

    def record_clean_shutdown(self) -> None:
        self.engine.leave("clean_shutdown", ())

    def record_unclean_shutdown(self, unsettled: tuple[str, ...]) -> None:
        self.engine.leave("unclean_shutdown", unsettled)

    def journal_shutdown_during_startup(self) -> None:
        self.engine.leave("shutdown_during_startup", ())

    def exit_process(self, code: int) -> None:
        self.engine.exit_code = code
        self.engine.wake()

    def cancel_all(self) -> None:
        """Cancel every armed timer: the engine is torn down."""
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        if self._poll is not None:
            self._poll.cancel()
            self._poll = None
