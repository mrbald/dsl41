"""One engine per run root: the `leader.lock` mutex and the state-machine version.

The mutex is an `flock` held for the process lifetime. It is not a lease: the kernel releases
it when the holder dies, `kill -9` included, so nothing decides whether the previous holder is
alive. It is taken before the log is read and before the first side effect (`resume_run`
replays, reconciles and appends), because a mutex taken after an act on the estate is not a
mutex (DL-99).

The lock file carries the holder's identity so a refusal can name it; the note is diagnostic,
never the fence, because a note can be stale and a held lock cannot.

Limit: a flock'd file serves one host, and on NFS it serves none.
"""

from __future__ import annotations

import errno
import json
import os
import socket

from datetime import datetime
from pathlib import Path
from typing import Protocol

from dsl41.runner_procid import LockHeld, flock_exclusive, mkdir_durable
from dsl41.runner_clock import EngineError

#: The version of the state machine that derives oracle state from inputs, the half
#: `catalog_hash` does not cover. The store's `meta.state_version` and every snapshot carry it,
#: and a restart requires an exact match (DL-424),
#: because mixed builds derive different revisions from identical inputs and nothing
#: downstream detects it: a SPAWN spec is a resolved literal command and the supervisor holds
#: no job definitions.
#:
#: Bump it when a change makes this build derive different state from an identical log:
#: oracle transitions, condition evaluation, timer ordering, the ss3 projection. A change a
#: replay cannot see does not bump it. It is not `dsl41_version`: refusing to resume a live
#: estate after a patch release would be an outage made by bookkeeping.
#: Each version below names the derivation it introduced.
#: 2 (DL-235): the ss4 stale-completion gate rejects a non-live row.
#: 3 (DL-241): term_run_time 0 arms no timer.
#: 4 (DL-242): box start resets member statuses; operator INACTIVE resolves and cascades.
#: 5 (DL-243): ON_ICE/ON_NOEXEC condition reads; FORCE_STARTJOB on a non-live ON_ICE or
#: ON_HOLD job.
#: 6 (DL-244): a holcal weekday date reaches the non_workday action and WORKDAYS excludes it.
#: `attest` re-derives a boundary's classification from the sealed catalogs, so a boundary
#: carries, refuses or assumes a job per the version that classified it.
#: 7 (DL-246): a box start decides a run_window member's disposition; a standalone window skip
#: moves a prior result to INACTIVE.
#: 8 (DL-247): an unset or zero priority and a forced start skip the machine-load check but
#: hold their load; a load waiter blocks lower priorities on its machine; more transitions
#: wake the queue.
#: 9 (DL-248): the schedule tick arms the relative must_complete deadline and its timer
#: carries the tick's run.
#: 10 (DL-249): run_window endpoints follow the vendor's DST rules.
#: 11 (DL-253): absolute must times arm alarms; must_start arms one deadline at a time.
#: 12 (DL-254): ON_ICE, ON_HOLD and ON_NOEXEC sent to a live (for ON_NOEXEC, iced) job are
#: ignored.
#: 13 (DL-255): a job waiting on a named resource blocks lower priorities that name it; a
#: start or enqueue that takes machine load owes a queue scan.
#: 14 (DL-256): a renewable request with no FREE frees its units on SUCCESS only; held units
#: outlive the run until RELEASE_RESOURCE or the job's next run; FORCE_STARTJOB of a holder
#: starts on them.
#: 15 (DL-259): a WEKR token selects a week of the year, and the wekr-first-week switch places
#: week 1. Autocal's day sets and the classification `attest` re-derives from them follow.
#: 16 (DL-260): start times follow the vendor's DST rules by default and the oracle names a
#: tick's slot by instant.
#: 17 (DL-285): an ON_ICE on a member that has not run in a RUNNING box runs the box's
#: completion check.
#: 18 (DL-301, SEM-14 and SEM-20's box clauses): a member taken off ice while its box runs
#: sits that run out; a box_terminator member that ends TERMINATED terminates its box. Each
#: is a default behind a switch.
#: 19 (DL-304, SEM-11's box-start completion moment): a box whose start leaves no member in
#: the run (every direct member on ice, or none) completes at its start under
#: `box-start-all-members-out=complete`, the default; an iced member still running, or
#: holding a running or queued job, keeps its box waiting; a start nested inside two starts of
#: the same job is refused.
#: 20 (DL-309, SEM-15 and SEM-12's run binding): under `idle-box-iced-member=ignore`, the
#: default, an idle box's recompute drops an iced member that is out of the run; the walk
#: from a job's transition up to its ancestors skips an ancestor that the same cascade
#: completed and started again.
#: 21 (DL-340): a member whose box restarts on its own completion settles the run that ended
#: when it starts again, by that run's policy (frees, keeps or spends its units), then
#: reserves or queues as any start does. A member the restart queues keeps its rank. A scan
#: continuation of a job the catalog does not define is dead, not pending, which the
#: classifier reads as no timer.
#: 22 (DL-345): the queue's scan addresses only a row in QUE_WAIT. A job that left QUE_WAIT
#: without dropping its rank (an injected STATUS, or a queued box's idle-box recompute) is
#: not cancelled or admitted by a scan. Where the box rules queue the job again (a box
#: restarted by its own completion), the rank it keeps is the same.
#: 23 (DL-348, SEM-14's job_terminator): a box's end FAILURE or TERMINATED kills its live
#: job_terminator members only while the run that ended is still the box's run. The kill
#: reads the box's run number before the status write, and the cascade compares it before
#: each member: once the write's wakes, or an earlier member's kill, started the box again,
#: the cascade reaches no further member, neither the new run's members nor a member carried
#: live into it, as the fold and the overrides leave them when the box's own run moves. The
#: bound ends the loop where such a member carries box_terminator and a TERMINATED end
#: terminates its box (the default).
#: 24 (DL-360): a job that enters QUE_WAIT takes a new rank. A box's end kills its
#: job_terminator members only while the box was not reset since: an ancestor's restart resets
#: a subbox to INACTIVE without moving its run, and the kill does not reach a live member that
#: the fold path leaves running. The default fold reads statuses: a member that has not run in
#: the box's run but holds a terminal status is done and votes with it. The status comes from a
#: member carried live into the run that then ended; an operator's SUCCESS, FAILURE or TERMINATED on
#: a member that waits; a KILLJOB on a member queued in the run, or made live by an injected
#: STARTING; or the idle recompute of a never-run subbox from statuses given to its members
#: (SEM-15). A resolution or off-ice mark decides only while the member reads INACTIVE.
STATE_MACHINE_VERSION = 24

LOCK_NAME = "leader.lock"


class Proof(Protocol):
    """What an appender re-proves before it writes, and drops when it stops.

    `Journal` and the adapters hold a `Proof`, never a `LeaderLock`: an appender proves that it
    still holds this run root's lock."""

    def check(self) -> None: ...

    def release(self) -> None: ...


class LeaderLock:
    """One directory's mutex, held for the process lifetime.

    `acquire` is exclusive and non-blocking: held, or refused with the holder named. `check`
    re-reads the proof (S6b): a replaced lock file leaves this process holding a lock on an
    unlinked inode while another holds the name, and losing the proof must stop the next
    append."""

    def __init__(
        self,
        directory: Path,
        name: str = LOCK_NAME,
        *,
        of: str = "run root",
        held_by: str = "engine",
    ) -> None:
        self.path = directory / name
        self.of = of
        self.held_by = held_by
        self._fd: int | None = None
        self._ino: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        try:
            fd = flock_exclusive(self.path)
        except LockHeld as exc:
            if exc.errno == errno.ESTALE:
                raise EngineError(
                    f"{self.path} was replaced while acquiring it: the lock excludes nobody;"
                    " retry, and find out what is deleting it"
                ) from exc
            raise EngineError(
                f"{self.path.parent} is held by another {self.held_by} ({self._holder()}):"
                f" one engine per {self.of} (DL-99)"
            ) from exc
        self._fd, self._ino = fd, os.fstat(fd).st_ino

    def note(self, *, at: datetime) -> None:
        """Record who holds the lock, for another process's refusal text."""
        if self._fd is None:
            raise EngineError(f"{self.path}: not held")
        # plain json.dumps, not canon.canonical_bytes: the blob feeds `_holder()` only and is
        # never digested
        blob = json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "since": at.isoformat(),
            },
            sort_keys=True,
        ).encode("utf-8")
        os.ftruncate(self._fd, 0)
        os.pwrite(self._fd, blob, 0)
        os.fsync(self._fd)

    def check(self) -> None:
        """Re-prove leadership (S6b) with one `stat`."""
        if self._fd is None:
            raise EngineError(f"{self.path}: leadership was never acquired")
        try:
            current = os.stat(self.path).st_ino
        except FileNotFoundError as exc:
            raise EngineError(
                f"{self.path} was deleted: this {self.held_by} can no longer prove it leads"
                f" this {self.of}, and another may already have claimed it (DL-101)"
            ) from exc
        if current != self._ino:
            raise EngineError(
                f"{self.path} was replaced: another {self.held_by} may hold this {self.of} (DL-101)"
            )

    def release(self) -> None:
        """Idempotent. The file stays: unlinking is what `check` exists to catch, and the kernel
        drops the lock when the fd closes."""
        if self._fd is None:
            return
        fd, self._fd, self._ino = self._fd, None, None
        os.close(fd)

    def _holder(self) -> str:
        try:
            note = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return "holder unknown"
        if not isinstance(note, dict):
            return "holder unknown"
        pid, host, since = note.get("pid"), note.get("host"), note.get("since")
        return f"pid {pid} on {host}, since {since}"


def acquire_run_root(run_root: Path) -> LeaderLock:
    """Take `run_root`'s lock before touching anything in it.

    For a caller whose first act is not opening the log. The engine's own entry points acquire
    when no caller has."""
    # every entry down to the run root is durable, so a power cut cannot lose the directory;
    # unconditional, so a retry after a failed fsync repairs it (mkdir_durable's contract)
    mkdir_durable(str(run_root))
    lock = LeaderLock(run_root)
    lock.acquire()
    return lock
