"""Owned-process, event-driven test barriers and bounded scenario supervision (DL-42)."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from typing import Any, BinaryIO

from dsl41.runner import Engine
from dsl41.runner_adapters import AdapterContext, Launch, LocalCommandAdapter

SCENARIO_S = 30.0
FAULT_WAIT_S = 0.5
STARTUP_S = 4.0
INVENTORY_S = 2.0
CLEANUP_JOINS_S = (3.0, 2.0, 1.0, 1.0)
CLEANUP_MARGIN_S = 1.0
CLEANUP_S = 4 * INVENTORY_S + sum(CLEANUP_JOINS_S) + CLEANUP_MARGIN_S
WATCHDOG_GRACE_S = STARTUP_S
FAULT_DEADLINES_S = {
    "no-waitid": SCENARIO_S + CLEANUP_S,
    "observer-failure": STARTUP_S + CLEANUP_S,
    "late-registration": SCENARIO_S + CLEANUP_S,
    "withheld-readiness": STARTUP_S + FAULT_WAIT_S + CLEANUP_S,
    "withheld-exit": STARTUP_S + FAULT_WAIT_S + CLEANUP_S,
    "inventory-failure": STARTUP_S + FAULT_WAIT_S + CLEANUP_S,
}
READINESS_FAULTS = {"withheld-readiness", "inventory-failure"}


def process_table() -> dict[int, tuple[int, int, str]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,stat="],
        capture_output=True,
        text=True,
        check=True,
        timeout=INVENTORY_S,
    )
    return {
        int(p): (int(parent), int(group), state)
        for p, parent, group, state in (line.split() for line in result.stdout.splitlines())
    }


def descendants(table: dict[int, tuple[int, int, str]], roots: set[int]) -> set[int]:
    owned = set(roots)
    while children := {p for p, (parent, _, _) in table.items() if parent in owned} - owned:
        owned.update(children)
    return owned


class _SpawnObserver:
    def __init__(self, observer: Any, recorded: Any) -> None:
        self.observer, self.recorded = observer, recorded

    def spawn_recorded(self, spawn: Any) -> None:
        self.observer.spawn_recorded(spawn)
        self.recorded()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.observer, name)


class RerunBarrier(LocalCommandAdapter):
    """The hook acknowledges a blocked read; release survives later registration (DL-42)."""

    def __init__(self, root: Path, *, before_record: bool, fault: str = "") -> None:
        super().__init__(grace_seconds=0.5)
        self.root, self.before_record, self.fault = root, before_record, fault
        self.ready, self.release = asyncio.Event(), asyncio.Event()
        self.initial_ready, self.arrived = asyncio.Event(), asyncio.Event()
        self.registration = asyncio.Event()
        if fault != "late-registration":
            self.registration.set()
        self.exit_ack = asyncio.Event()
        self.exit_withheld = asyncio.Event()
        self.exited: set[Path] = set()
        self.launches: dict[str, Launch] = {}
        self.all_launches: dict[Path, Launch] = {}
        self.acknowledged: set[Path] = set()
        self.recorded: set[Path] = set()
        self.pipes: dict[Path, tuple[BinaryIO, BinaryIO]] = {}
        self.owned: set[int] = set()
        self.blocked: dict[Path, str] = {}
        self.root.mkdir()

    async def observe(
        self, job_ir: Any, run_number: int, ctx: AdapterContext, launch: Launch
    ) -> None:
        assert ctx.spool is not None
        spool = ctx.spool
        self.all_launches[spool] = launch
        original = launch.observer

        def recorded() -> None:
            if spool.name == "1":
                self.recorded.add(spool)
                if len(self.recorded) == 20:
                    self.initial_ready.set()

        launch.observer = _SpawnObserver(original, recorded)
        if spool.name == "2":
            directory = self.root / spool.parent.name
            directory.mkdir()
            for name in ("ack", "release"):
                os.mkfifo(directory / name)
            self.pipes[spool] = tuple(
                os.fdopen(os.open(directory / name, os.O_RDWR | os.O_NONBLOCK), "r+b", buffering=0)
                for name in ("ack", "release")
            )
            ctx.env.update(
                BARRIER_PIPES=str(directory),
                BARRIER_PAUSE=("post_spawn_pre_record" if self.before_record else "post_record"),
            )
        if self.fault == "no-waitid":
            ctx.env["BARRIER_NO_WAITID"] = "1"
        try:
            await super().observe(job_ir, run_number, ctx, launch)
        finally:
            self.blocked.pop(spool, None)
            launch.observer = original

    def remember_processes(self) -> None:
        wrappers = {
            launch.proc.pid for launch in self.all_launches.values() if launch.proc is not None
        }
        self.owned.update(wrappers)
        for spool in self.all_launches:
            record = spool / "spawn.json"
            if record.exists():
                spawn = json.loads(record.read_text())
                self.owned.update((spawn["wrapper_pid"], spawn["command_pid"]))
        try:
            self.owned.update(descendants(process_table(), wrappers))
        finally:
            (self.root / "owned.json").write_text(json.dumps(sorted(self.owned)))

    async def _watch(
        self, run_dir: Path, run_id: str, proc: asyncio.subprocess.Process, launch: Launch
    ) -> None:
        self.owned.add(proc.pid)
        (self.root / "owned.json").write_text(json.dumps(sorted(self.owned)))
        if run_dir.name == "2":
            if self.fault == "observer-failure":
                raise RuntimeError("injected observer failure")
            ack, release = self.pipes[run_dir]
            paused = asyncio.get_running_loop().create_future()

            def acknowledged() -> None:
                if ack.read(64) and not paused.done():
                    paused.set_result(None)

            loop = asyncio.get_running_loop()
            loop.add_reader(ack, acknowledged)
            self.blocked[run_dir] = "pause-acknowledgement"
            try:
                await paused
            finally:
                loop.remove_reader(ack)
            self.acknowledged.add(run_dir)
            self.remember_processes()
            if len(self.acknowledged) == 2:
                self.arrived.set()
            self.blocked[run_dir] = "registration"
            await self.registration.wait()
            self.launches[run_dir.parent.name] = launch
            if self.release.is_set():
                release.write(b"r")
            if len(self.launches) == 2 and self.fault not in READINESS_FAULTS:
                self.ready.set()
            if self.before_record:
                self.blocked[run_dir] = "release"
                await self.release.wait()
                self.blocked[run_dir] = "wrapper-exit"
                await proc.wait()
                if self.fault == "withheld-exit":
                    self.blocked[run_dir] = "exit-acknowledgement"
                    self.exited.add(run_dir)
                    if len(self.exited) == 2:
                        self.exit_withheld.set()
                    await self.exit_ack.wait()
        self.blocked[run_dir] = "spool-observation"
        try:
            await super()._watch(run_dir, run_id, proc, launch)
        finally:
            self.blocked.pop(run_dir, None)

    def resume(self) -> None:
        if not self.release.is_set():
            self.release.set()
            for spool in self.acknowledged:
                if spool.parent.name in self.launches:
                    self.pipes[spool][1].write(b"r")

    def emergency_release(self) -> None:
        self.resume()
        self.registration.set()
        for _, release in self.pipes.values():
            release.write(b"r")
        for launch in self.all_launches.values():
            launch.close_lifeline()

    @property
    def open_fifos(self) -> list[str]:
        return [
            f"{spool}/{name}"
            for spool, pair in self.pipes.items()
            for name, stream in zip(("ack", "release"), pair, strict=True)
            if not stream.closed
        ]

    def close(self) -> None:
        for pair in self.pipes.values():
            for stream in pair:
                stream.close()


class Supervision:
    def __init__(self, engine: Engine, barrier: RerunBarrier) -> None:
        self.engine, self.barrier = engine, barrier
        self.task = asyncio.create_task(engine.run_until_quiescent(datetime.max))
        self.phase = "starting"
        self.waits: dict[asyncio.Future[Any], str] = {}
        self.inventory_errors: list[Exception] = []
        self.skipped_signals: list[dict[str, object]] = []
        self.shutdown_task: asyncio.Task[None] | None = None

    def diagnostic(self) -> str:
        task = self.task
        exception = (
            repr(task.exception())
            if task.done() and not task.cancelled()
            else ("cancelled" if task.cancelled() else "pending")
        )
        attempts = [
            {"attempt": str(path), "pid": launch.proc.pid if launch.proc else None}
            for path, launch in self.barrier.all_launches.items()
        ]
        return json.dumps(
            dict(
                phase=self.phase,
                attempts=attempts,
                engine_task_exception=exception,
                inventory_errors=[repr(error) for error in self.inventory_errors],
                skipped_signals=self.skipped_signals,
                outstanding_waits=[label for wait, label in self.waits.items() if not wait.done()]
                + [
                    {
                        "attempt": str(path),
                        "pid": self.barrier.all_launches[path].proc.pid,
                        "wait": phase,
                    }
                    for path, phase in self.barrier.blocked.items()
                    if self.barrier.all_launches[path].proc is not None
                ],
            )
        )

    async def wait(self, operation: Any, phase: str, *, timeout: float = SCENARIO_S) -> Any:
        self.phase = phase
        pending = asyncio.ensure_future(operation)
        self.waits[pending] = phase
        try:
            done, _ = await asyncio.wait(
                {pending, self.task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if self.task in done:
                self.task.result()
                if pending not in done:
                    raise RuntimeError("engine ended before " + phase)
            if pending not in done:
                raise TimeoutError(self.diagnostic())
            return pending.result()
        except BaseException as exc:
            exc.add_note(self.diagnostic())
            raise
        finally:
            if pending is not self.task and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            self.waits.pop(pending, None)

    def inventory(self, operation: Any) -> Any:
        try:
            return operation()
        except (OSError, subprocess.SubprocessError) as exc:
            self.inventory_errors.append(exc)
            return None

    def kill_wrappers(self, procs: list[asyncio.subprocess.Process]) -> None:
        live = {proc.pid: proc for proc in procs if proc.returncode is None}
        self.skipped_signals.extend(
            {"pid": pid, "reason": "no live process handle"}
            for pid in sorted(self.barrier.owned - live.keys())
        )
        for proc in live.values():
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    async def cleanup(self) -> None:
        # Process inventories and four bounded joins fit CLEANUP_S (DL-42).
        tasks: set[asyncio.Future[Any]] = {self.task}
        try:
            self.barrier.emergency_release()
            self.task.cancel()
            self.shutdown_task = asyncio.create_task(self.engine.shutdown())
            tasks.add(self.shutdown_task)
            procs = [
                launch.proc
                for launch in self.barrier.all_launches.values()
                if launch.proc is not None
            ]
            waiters = {asyncio.create_task(proc.wait()): proc for proc in procs}
            tasks.update(waiters)
            self.inventory(self.barrier.remember_processes)
            _, pending = await asyncio.wait(tasks, timeout=CLEANUP_JOINS_S[0])
            if pending & waiters.keys():
                self.kill_wrappers([waiters[waiter] for waiter in pending & waiters.keys()])
                _, pending = await asyncio.wait(pending, timeout=CLEANUP_JOINS_S[1])
            if pending:
                _, pending = await asyncio.wait(pending, timeout=CLEANUP_JOINS_S[2])
            for task in pending:
                task.cancel()
            if pending:
                _, pending = await asyncio.wait(pending, timeout=CLEANUP_JOINS_S[3])
            assert not pending, "cleanup deadline: " + self.diagnostic()
            self.inventory(self.barrier.remember_processes)
            table = self.inventory(process_table)
            if table is not None:
                survivors = self.barrier.owned & table.keys()
                assert not survivors, f"surviving owned processes: {survivors}; {self.diagnostic()}"
            for task in tasks:
                if not task.cancelled():
                    exception = task.exception()
                    if task is self.shutdown_task and exception is not None:
                        raise exception
            if self.inventory_errors:
                error = self.inventory_errors[0]
                error.add_note(self.diagnostic())
                raise error
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
                elif not task.cancelled():
                    task.exception()
            try:
                if self.engine.journal is not None:
                    self.engine.journal.close()
            finally:
                self.barrier.close()
