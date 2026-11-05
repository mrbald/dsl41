"""The site's lossy notification boundary (DL-446)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, field_validator

from dsl41.runtime_policy import hook_max_run_alarm

if TYPE_CHECKING:
    from dsl41.oracle_state import StatusObservation
    from dsl41.runner import Engine


class HookSettings(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)

    status_hook: str | None = None
    status_hook_long_running_s: int = 0

    @field_validator("status_hook")
    @classmethod
    def executable_path(cls, value: str | None) -> str | None:
        if value is not None and (not value or not os.path.isabs(value) or "\x00" in value):
            raise ValueError("status_hook must be an absolute executable path without NUL")
        return value

    @field_validator("status_hook_long_running_s")
    @classmethod
    def threshold(cls, value: int) -> int:
        if value < 0:
            raise ValueError("status_hook_long_running_s must be nonnegative")
        return value


def payload(
    event: str, run_root: Path | None, *, at: datetime | None = None, **values: Any
) -> bytes:
    """One versioned object, newline and EOF; null means unavailable (DL-446)."""
    stamp = at or datetime.now(UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    row = {
        "v": 1,
        "id": str(uuid4()),
        "event": event,
        "at": stamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "host": socket.gethostname(),
        "run_root": None if run_root is None else str(run_root.absolute()),
        "job": None,
        "run_number": None,
        "transition": None,
        "exit_code": None,
        "unit": None,
        "threshold_s": None,
        "alarm_if_fail": True if event == "job_failure" else None,
        "alarm_if_terminated": True if event == "job_terminated" else None,
    }
    row.update(values)
    return (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _group_alive(pgid: int, timeout: float) -> bool:
    """Prove group absence, ignoring zombies; an unreadable group stays live (DL-446)."""
    if sys.platform.startswith("linux"):
        from dsl41.runner_procid import linux_group_has_live_member

        return linux_group_has_live_member(pgid)
    try:
        result = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=,pgid=,stat="],
            capture_output=True,
            text=True,
            timeout=max(0.001, timeout),
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            close_fds=True,
        )
        if result.returncode:
            return True
        missing = {1, os.getpid()}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 3 or not all(v.isdigit() for v in fields[:2]):
                return True
            missing.discard(int(fields[0]))
            if int(fields[1]) == pgid and not fields[2].startswith("Z"):
                return True
        return bool(missing)
    except (OSError, subprocess.SubprocessError):
        return True


class _Invocation:
    """One daemon owner survives a stalled spawn and even loop shutdown (DL-446)."""

    def __init__(self, path: str, data: bytes, deadline: float) -> None:
        self.path, self.data, self.deadline = path, data, deadline
        self.stop = threading.Event()
        self.done = threading.Event()
        self.clean = False
        self.reason: str | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.thread = threading.Thread(target=self.run, daemon=True, name="dsl41-status-hook")

    def _deliver(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdin is not None
        fd = proc.stdin.fileno()
        os.set_blocking(fd, False)
        position = 0
        while not self.stop.is_set() and time.monotonic() < self.deadline:
            # Hold the unreaped leader's PID until group cleanup has finished (DL-446).
            if hasattr(os, "waitid"):
                ended = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if ended is not None:
                    return
            else:
                from dsl41.runner_procid import proc_is_zombie

                if proc_is_zombie(proc.pid):
                    return
            if not proc.stdin.closed:
                try:
                    position += os.write(fd, self.data[position:])
                except BlockingIOError:
                    pass
                except BrokenPipeError:
                    proc.stdin.close()
                if position == len(self.data):
                    proc.stdin.close()
            self.stop.wait(min(0.01, max(0, self.deadline - time.monotonic())))
        if not self.stop.is_set():
            self.reason = "delivery failed"

    def _cleanup(self, proc: subprocess.Popen[bytes]) -> bool:
        """TERM, half a second, KILL, half a second to prove absence and reap (DL-446)."""
        from dsl41.runner_procid import killpg_quiet

        end = time.monotonic() + 1
        term_end = end - 0.5
        try:
            killpg_quiet(proc.pid, signal.SIGTERM)
            while time.monotonic() < term_end:
                if not _group_alive(proc.pid, term_end - time.monotonic()):
                    break
                time.sleep(0.01)
            killpg_quiet(proc.pid, signal.SIGKILL)
            while time.monotonic() < end:
                if not _group_alive(proc.pid, end - time.monotonic()):
                    proc.wait(timeout=max(0, end - time.monotonic()))
                    return True
                time.sleep(0.01)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            proc.wait(timeout=0)
        except (OSError, subprocess.SubprocessError):
            pass
        return False

    def run(self) -> None:
        try:
            if self.stop.is_set() or time.monotonic() >= self.deadline:
                self.clean = True
                return
            proc = self.process = subprocess.Popen(
                [self.path],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd="/",
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                umask=0o077,
                close_fds=True,
                start_new_session=True,
            )
            self._deliver(proc)
        except Exception:
            self.reason = "delivery failed"
        finally:
            owned = self.process
            if owned is None:
                self.clean = True
            else:
                try:
                    self.clean = self._cleanup(owned)
                    if self.clean and owned.returncode and self.reason is None:
                        self.reason = "nonzero exit"
                except Exception:
                    self.reason = "cleanup failed"
                finally:
                    try:
                        if owned.stdin is not None:
                            owned.stdin.close()
                    except Exception:
                        pass
            self.done.set()


class HookDelivery:
    """One FIFO worker; delivery and diagnostics cannot affect the engine (DL-446)."""

    def __init__(self, path: str, run_root: Path | None) -> None:
        self.path = path
        self.run_root = run_root
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self.worker: asyncio.Task[None] | None = None
        self.disabled = False
        self.counts: dict[str, int] = {}
        self._last_log = float("-inf")
        self._invocation: _Invocation | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def diagnostic(self, reason: str, *, force: bool = False) -> None:
        try:
            self.counts[reason] = self.counts.get(reason, 0) + 1
            now = time.monotonic()
            if force or now - self._last_log >= 60:
                self._last_log = now
                logging.getLogger(__name__).warning(
                    "status hook: %s; counts=%s", reason, self.counts
                )
        except Exception:
            pass

    def enqueue(self, event: str, **values: Any) -> None:
        try:
            if self.disabled:
                return
            data = payload(event, self.run_root, **values)
            if len(data) > 16 * 1024:
                self.diagnostic("oversize")
                return
            try:
                self.queue.put_nowait(data)
            except asyncio.QueueFull:
                self.diagnostic("overflow")
                return
            if self.worker is None:
                self.worker = asyncio.create_task(self._work())
        except Exception:
            self.diagnostic("enqueue failed")

    def observe(self, observations: list[StatusObservation]) -> None:
        for row in observations:
            self.enqueue(
                row.event,
                at=row.at,
                job=row.job,
                run_number=row.run_number,
                transition=row.transition,
                exit_code=row.exit_code,
                alarm_if_fail=row.alarm_if_fail,
                alarm_if_terminated=row.alarm_if_terminated,
            )

    async def _run(self, invocation: _Invocation) -> None:
        invocation.thread.start()
        while not invocation.done.is_set():
            await asyncio.sleep(0.01)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _invoke(self, data: bytes) -> None:
        deadline = time.monotonic() + 5
        invocation = self._invocation = _Invocation(self.path, data, deadline)
        task = asyncio.create_task(self._run(invocation))
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        try:
            await asyncio.wait_for(asyncio.shield(task), max(0, deadline - time.monotonic()))
        except asyncio.CancelledError:
            raise
        except Exception:
            invocation.reason = "delivery failed"
        finally:
            invocation.stop.set()
            cleanup_end = min(deadline + 1, time.monotonic() + 1)
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    max(0, cleanup_end - time.monotonic()),
                )
            except (Exception, asyncio.CancelledError):
                pass
            if invocation.reason:
                self.diagnostic(invocation.reason)
            if not invocation.done.is_set() or not invocation.clean:
                self.disabled = True
                self.diagnostic("cleanup unconfirmed; delivery disabled", force=True)
            else:
                self._invocation = None

    async def _work(self) -> None:
        while not self.disabled:
            data = await self.queue.get()
            try:
                await self._invoke(data)
            finally:
                self.queue.task_done()

    async def close(self) -> None:
        self.disabled = True
        if self._invocation is not None:
            self._invocation.stop.set()
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)


class StatusHooks:
    def __init__(self, engine: Engine, settings: HookSettings) -> None:
        assert settings.status_hook is not None
        self.engine = engine
        self.delivery = HookDelivery(settings.status_hook, engine.run_root or Path.cwd())
        self.threshold = settings.status_hook_long_running_s
        self._seen: dict[str, tuple[int, datetime | None]] = {}
        self.monitor = asyncio.create_task(self._monitor())

    def check_long_running(self) -> None:
        try:
            wall = self.engine.clock.now()
            frontier = self.engine.frontiers.at
            now = max(wall, frontier) if frontier is not None else wall
            for job, row in self.engine.oracle.store.job.items():
                interval = (row.run_number, row.status_at)
                if row.status != "RUNNING":
                    self._seen.pop(job, None)
                    continue
                if row.status_at is None or self._seen.get(job) == interval:
                    continue
                minutes = hook_max_run_alarm(self.engine.oracle.catalog.jobs.get(job))
                threshold = minutes * 60 if minutes else self.threshold
                if not threshold or (now - row.status_at).total_seconds() < threshold:
                    continue
                current = self.engine.oracle.store.runtime(job)
                if (
                    current.status != "RUNNING"
                    or (current.run_number, current.status_at) != interval
                ):
                    continue
                self._seen[job] = interval
                self.delivery.enqueue(
                    "job_long_running",
                    at=now,
                    job=job,
                    run_number=current.run_number,
                    threshold_s=threshold,
                )
        except Exception:
            self.delivery.diagnostic("long-running check failed")

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(1)
            self.check_long_running()

    async def close(self) -> None:
        if self.monitor is not None:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        await self.delivery.close()


async def notify_once(path: str, run_root: Path | None, event: str, **values: Any) -> None:
    delivery = HookDelivery(path, run_root)
    delivery.enqueue(event, **values)
    try:
        await delivery.queue.join()
    finally:
        await delivery.close()


def main() -> None:
    """Installed OnFailure helper: literal arguments, no config or store access (DL-446)."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hook")
    parser.add_argument("run_root", type=Path)
    parser.add_argument("unit")
    args = parser.parse_args()
    settings = HookSettings(status_hook=args.hook)
    asyncio.run(
        notify_once(settings.status_hook or "", args.run_root, "engine_down", unit=args.unit)
    )


if __name__ == "__main__":
    main()
