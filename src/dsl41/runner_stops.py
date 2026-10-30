"""The engine's side of the attempt and engine lifecycle tiers (spec-wiring 2b, DL-426).

`runner_attempts` holds the two tiers and asks the world for what they need through two
protocols, `Processes` and `EngineEffects`. `EngineStops` is the engine's implementation of
both: it runs the stop ladder on a recorded group, arms the tiers' timers on the event loop,
reads an attempt's spool, and hands each outcome and each lifecycle intent back to the engine,
which journals it and carries out its plan. It looks for the groups of the held attempts, whose
jobs start nothing until they are gone. `Observer` turns one attempt's adapter reports into the
attempt tier's observations.

The timers run on the process's monotonic clock, as the ladder does: the drain's deadline and
an attempt's `record_timeout` bound real processes, whatever clock domain the engine reads.
A timer that fires posts through the tier at once; the loop's next iteration carries out what
it implied (DL-426).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from dsl41 import runner_procid as _procid
from dsl41.engine_charts import AttemptOutcome
from dsl41.runner_adapters import Launch, group_has_live_member, spool_record, stop_ladder
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
    #: set once the attempt ended, or once its stop could not be carried out
    settled: asyncio.Event = field(default_factory=asyncio.Event)


class Observer:
    """One attempt's adapter reports, posted to the attempt tier under its run id."""

    def __init__(self, stops: EngineStops, run_id: str) -> None:
        self.stops = stops
        self.run_id = run_id

    def spawn_recorded(self, spawn: Mapping[str, Any]) -> None:
        self.stops.note_boot(spawn)
        self.stops.engine.attempts.spawn_recorded(self.run_id, spawn)

    def status_recorded(self, status: Mapping[str, Any]) -> None:
        self.stops.engine.attempts.status_recorded(self.run_id, status)

    def spawn_failed(self, error: str) -> None:
        self.stops.engine.attempts.spawn_failed(self.run_id, error)

    def wrapper_exited(self, spawn: Mapping[str, Any] | None) -> None:
        if spawn is not None:
            self.stops.note_boot(spawn)
        self.stops.engine.attempts.wrapper_exited(self.run_id, spawn)


class EngineStops:
    """`Processes` and `EngineEffects` for one engine (module docstring)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.boot_id = _procid.current_boot_id()
        #: the boot each recorded command group was launched under
        self._boots: dict[tuple[int, str], object] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        #: the groups a ladder has signalled: one of them still existing outlived its stop
        self._laddered: set[int] = set()
        #: the poll that looks for the held attempts' groups, while any is held
        self._poll: asyncio.TimerHandle | None = None

    # -- the command's liveness

    def note_boot(self, spawn: Mapping[str, Any]) -> None:
        """Keep the boot a `spawn.json` names for the group it names."""
        pgid, token = spawn.get("command_pgid"), spawn.get("command_start_time")
        if isinstance(pgid, int) and isinstance(token, str):
            self._boots[(pgid, token)] = spawn.get("boot_id", self.boot_id)

    def command_alive(self, pgid: int, start_token: str) -> bool:
        """Whether the recorded command still runs: a group recorded under another boot is
        dead whatever the pid space says now (DL-42 item 5); else the pid-reuse guard decides,
        and a group a ladder signalled that still has a live member after it has survived its
        stop."""
        if self._boots.get((pgid, start_token), self.boot_id) != self.boot_id:
            return False
        if _procid.verify_alive(pgid, start_token):
            return True
        return pgid in self._laddered and group_has_live_member(pgid)

    # -- Processes

    async def ladder(self, pgid: int, start_token: str, grace_term_s: float) -> None:
        self._laddered.add(pgid)
        await stop_ladder(pgid, start_token, grace_term_s)

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
        run, DL-118) stops the engine at the loop's next turn, or fails startup's step 5,
        instead of vanishing into the event loop's handler."""
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
        nothing until its group is gone, and the log and `query` say why (DL-426)."""
        facts = self.engine.attempt_facts(attempt)
        facts.note = note
        facts.settled.set()
        group = self.engine.attempts.group(attempt)
        assert group is not None  # a ladder or a timer reached only a recorded group
        self.engine.hold_group(attempt, group[0])
        best_effort_report(f"dsl41: {facts.job}.{facts.run_number}.{facts.ordinal}: {note}")

    # -- the held groups

    def watch_groups(self) -> None:
        """Look for the held attempts' groups every `GROUP_POLL_S`, while any is held."""
        if self._poll is None:
            self._poll = asyncio.get_running_loop().call_later(GROUP_POLL_S, self._look)

    def _look(self) -> None:
        """Release each held attempt whose group has no live member left. What that raises
        stops the engine at the loop's next turn (`_record_timeout`)."""
        self._poll = None
        try:
            for attempt, pgid in list(self.engine.held_groups.items()):
                if not group_has_live_member(pgid):
                    self.engine.group_gone(attempt)
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
