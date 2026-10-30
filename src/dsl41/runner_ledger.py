"""Leadership over one run root (concurrency-model ss1/ss7, stage S6a).

Three of ss1's five ledger capabilities live here: monotone epoch allocation, the
linearizable read of the leader record, and the mutex both rest on. The other two landed with
S2: decision lookup by `request_id` is the `DecisionIndex`; atomic multi-record commit is the
one-line attempt record.

The mutex is an `flock` held for the process lifetime. It is not a lease: the kernel releases
it when the holder dies, `kill -9` included, so nothing decides whether the previous holder is
alive. It is taken before the log is read and before the first side effect (`resume_run`
replays, reconciles, re-drives recorded kills and appends), because a mutex taken after an act
on the estate is not a mutex (DL-99).

The leader record lives in the ledger: the epoch is allocated by appending it, so the
allocation and the log's account of it cannot disagree. The lock file carries the holder's
identity so a refusal can name it; the note is diagnostic, never the fence, because a note can
be stale and a held lock cannot.

Limit: a flock'd file with fsync provides the three rows for one host and none across hosts,
and on NFS it provides none. The relay DL-97 defers waits on a Postgres-class store; this module
gives it a fencing token that moves.
"""

from __future__ import annotations

import errno
import json
import os
import socket

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, overload

from dsl41.canon import is_wire_int
from dsl41.ir import CatalogIR
from dsl41.run_root import catalog_hash_for
from dsl41.runner_procid import LockHeld, flock_exclusive, mkdir_durable
from dsl41.runner_clock import EngineError

#: ss7 ledger header, the half `catalog_hash` does not cover: the version of the state
#: machine that derives oracle state from inputs. Leader eligibility requires an exact match,
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

    `Journal` and the adapters hold a `Proof`, never a `LeaderLock`: an appender proves two
    things (period-model ss1.3), that it leads this run root and that it leads the lineage."""

    def check(self) -> None: ...

    def release(self) -> None: ...


class LeaderLock:
    """One directory's mutex, held for the process lifetime.

    `acquire` is ss7's ACQUIRE: exclusive, non-blocking, held or refused with the holder named.
    `check` re-reads the proof (S6b): a replaced lock file leaves this process holding a lock
    on an unlinked inode while another holds the name, and ss7's "losing proof stops dispatch"
    needs that noticed.

    The lineage lock is this pattern on the anchor directory (DL-133, period-model ss15's
    row), because a bare `O_EXCL` does not solve replacement and lifetime. Only the file name
    and the noun in the refusal differ; the run-root spelling is the default."""

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
                f" one leader per {self.of} (concurrency-model ss7)"
            ) from exc
        self._fd, self._ino = fd, os.fstat(fd).st_ino

    def note(self, *, epoch: int, at: datetime) -> None:
        """Record who holds this term, for another process's refusal text."""
        if self._fd is None:
            raise EngineError(f"{self.path}: not held")
        # plain json.dumps, not canon.canonical_bytes: the blob feeds `_holder()` only and is
        # never digested
        blob = json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "epoch": epoch,
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
                f" this {self.of}, and another may already have claimed it"
                " (concurrency-model ss7)"
            ) from exc
        if current != self._ino:
            raise EngineError(
                f"{self.path} was replaced: another {self.held_by} may hold this {self.of}"
                " (concurrency-model ss7)"
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
        pid, host = note.get("pid"), note.get("host")
        epoch, since = note.get("epoch"), note.get("since")
        return f"pid {pid} on {host}, epoch {epoch}, since {since}"


def acquire_run_root(run_root: Path) -> LeaderLock:
    """Take leadership of `run_root` before touching anything in it.

    For a caller whose first act is not opening the log, such as `dsl41 run --detached`, which
    starts a supervisor. The engine's own entry points acquire when no caller has."""
    # every entry down to the run root is durable, so a power cut cannot lose the directory;
    # unconditional, so a retry after a failed fsync repairs it (mkdir_durable's contract)
    mkdir_durable(str(run_root))
    lock = LeaderLock(run_root)
    lock.acquire()
    return lock


class Fence:
    """Every proof this process holds, re-proved together (period-model ss1.3).

    An appender leads this run root and the lineage the anchor names. Deleting or replacing
    either directory stops the incumbent on its next act (DL-101); that records a divergence
    and does not un-run what happened. A root with no anchor holds a fence of one lock."""

    def __init__(self, *locks: LeaderLock) -> None:
        self.locks = locks

    def check(self) -> None:
        for lock in self.locks:
            lock.check()

    def release(self) -> None:
        for lock in self.locks:
            lock.release()


def next_epoch(records: list[dict[str, Any]]) -> int:
    """ss1's monotone allocation: one past the highest term the log records, read under the lock.

    A journal with no `leader` record (S6a) gets term 1, as a fresh run root does."""
    terms = [int(r["epoch"]) for r in records if r.get("rec") == "leader" and "epoch" in r]
    return max(terms, default=0) + 1


@overload
def check_state_machine_version(opening: Mapping[str, Any], *, mode: Literal["lead"]) -> None: ...


@overload
def check_state_machine_version(
    opening: Mapping[str, Any], *, mode: Literal["replay"], where: str = ""
) -> None: ...


def check_state_machine_version(
    opening: Mapping[str, Any], *, mode: Literal["lead", "replay"], where: str = ""
) -> None:
    """Refuse a log whose version this build does not implement (period-model ss2.1,
    concurrency-model ss7): a foreign build neither leads nor replays it.

    `mode` selects the refusal text: "lead" at resume (`check_leader_eligibility`), "replay" in
    every offline reader (`check_replay_version`). An absent field refuses in both modes
    (DL-138, DL-189; `read_journal` already refuses an unversioned segment, DL-165). The
    version is read exactly: `is_wire_int` rejects `bool`, so a JSON `true` cannot stand in for
    version 1 (DL-151). `where` names the segment in the replay message; the overloads make it
    a type error in lead mode."""
    pinned = opening.get("state_machine_version")
    if is_wire_int(pinned) and pinned == STATE_MACHINE_VERSION:
        return
    if mode == "lead":
        raise EngineError(
            f"state-machine version mismatch: this journal is v{pinned}, this build"
            f" derives v{STATE_MACHINE_VERSION}; a leader must derive identical"
            " revisions from identical inputs (concurrency-model ss7) -- re-baseline"
            " explicitly with a fresh run"
        )
    named = f"{where}: " if where else ""
    raise EngineError(
        f"{named}this segment names state_machine_version {pinned!r} and this build"
        f" derives v{STATE_MACHINE_VERSION} -- one executable implements one state"
        " machine, so this build can neither lead nor replay this log"
        " (period-model ss2.1)"
    )


def check_leader_eligibility(opening: dict[str, Any], *, catalog: CatalogIR) -> None:
    """ss7: leading requires an exact match on the log's two opening pins, checked together.

    The catalog hash is recomputed under the recipe the log's own `segment` names
    (period-model ss1.1), so a gate never refuses an estate that did not change (DL-100).
    The version half runs first (`check_state_machine_version`, DL-189), so a log an older
    build wrote is refused for its version, not reported as a changed estate. The catalog hash
    covers `ir_version`, so an IR bump alone moves it (DL-253)."""
    check_state_machine_version(opening, mode="lead")
    if opening.get("catalog_hash") != catalog_hash_for(opening, catalog):
        raise EngineError(
            "catalog hash mismatch: the estate changed since this journal was written;"
            " re-baseline explicitly with a fresh run (no silent semantic drift, ss7)"
        )
