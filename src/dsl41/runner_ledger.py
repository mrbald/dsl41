"""Leadership over one run root (concurrency-model ss1/ss7, stage S6a).

Three of ss1's five ledger capabilities live here: monotone epoch
allocation, the linearizable read of the leader record that allocation
depends on, and the mutex both rest on. The other two landed with S2 --
decision lookup by `request_id` is the `DecisionIndex`, and atomic
multi-record commit is the one-line attempt record.

**The mutex is an `flock` held for the process lifetime.** Not a lease: it
has no expiry to renew, because the kernel releases it when the holder
dies, `kill -9` included. That is what the control socket's connect probe
was approximating and getting wrong in both directions -- an engine wedged
past its 200ms timeout loses its socket to a second engine, and a socket
left behind by a crash has to be unlinked on a guess. Nothing here has to
decide whether the previous holder is alive.

**It is taken before the log is read, not after.** Reading first would let
another process append between the read and the acquire, so the epoch this
one allocates would be one the log already used. More importantly it is
taken before the first side effect: `resume_run` replays, reconciles,
re-drives recorded kills and appends, and every one of those is an act on
the estate. A mutex taken after them is not a mutex (DL-99).

**The leader record lives in the ledger,** for ss1's own reason about the
outbox: the epoch is allocated BY appending it, so the allocation and the
log's account of it cannot disagree, and no crash leaves one without the
other. The lock file carries the holder's identity so a refusal can name
who holds it -- diagnostics, never the fence, because a note can be stale
and a held lock cannot.

**Where this stops.** ss1 asks for a Postgres-class store and says
"whatever provides it": a flock'd file with fsync provides all three rows
for ONE host and none of them across hosts. It is not a linearizable
leader record for a second machine, and on NFS it is not one at all. The
relay DL-97 deferred still waits on that store; what it gets from here is
a fencing token that moves.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from dsl41.ir import CatalogIR
from dsl41.period import catalog_hash_for
from dsl41.runner_procid import mkdir_durable
from dsl41.runner_clock import EngineError

#: ss7 ledger header, the half `catalog_hash` does not cover: the version of
#: the state machine that derives oracle state from inputs. Leader
#: eligibility requires an exact match, because mixed builds derive
#: different revisions from identical inputs and nothing downstream can
#: detect the disagreement -- a SPAWN spec is a resolved literal command
#: string and the supervisor holds no job definitions.
#:
#: BUMP IT when a change makes this build derive different state from an
#: identical log: oracle transitions, condition evaluation, timer ordering,
#: the ss3 projection. Do NOT bump it for anything a replay cannot see. It
#: is deliberately not `dsl41_version`, which moves for a docs typo --
#: refusing to resume a live estate after a patch release would be an outage
#: manufactured by bookkeeping.
STATE_MACHINE_VERSION = 1

#: absent from a journal written before S6a, on the courtesy S2 gave a
#: journal with no `request_id`: it was written by the build that defined
#: version 1, so that is what it pinned.
_ASSUMED_VERSION = 1

LOCK_NAME = "leader.lock"


class Proof(Protocol):
    """What an appender re-proves before it writes, and drops when it stops.

    `Journal` and the adapters hold one of these, never a `LeaderLock`
    directly: since period-model ss1.3 an appender must prove TWO things --
    that it leads this run root and that it still leads the lineage -- and
    a writer typed to the single lock could only ever check one of them."""

    def check(self) -> None: ...

    def release(self) -> None: ...


class LeaderLock:
    """One directory's mutex, held for the process lifetime.

    `acquire` is ss7's ACQUIRE on this substrate: exclusive, non-blocking,
    and either held or refused with the holder named. `check` is the
    positive proof re-read (S6b) -- ss7's "losing proof stops dispatch"
    needs a way to notice, and on a filesystem the way to lose it is for
    the lock file to be replaced under us, leaving this process holding an
    exclusive lock on an unlinked inode while another holds one on the
    name.

    **Generalized to the anchor by DL-133** (period-model ss15's row): the
    lineage lock is this pattern on the anchor directory, because that
    pattern already solves what a bare `O_EXCL` does not -- replacement and
    lifetime. Only the file name and the noun in the refusal move; the
    run-root spelling is the default, so a run root's `leader.lock` is
    byte-identical to what every earlier build wrote."""

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
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise EngineError(
                f"{self.path.parent} is held by another {self.held_by} ({self._holder()}):"
                f" one leader per {self.of} (concurrency-model ss7)"
            ) from exc
        ino = os.fstat(fd).st_ino
        try:
            current = os.stat(self.path).st_ino
        except FileNotFoundError:
            current = None
        if current != ino:
            # the file we locked is no longer the file at that name: our lock
            # is on an unlinked inode and excludes nobody. Refuse rather than
            # run as a leader that cannot prove it is one.
            os.close(fd)
            raise EngineError(
                f"{self.path} was replaced while acquiring it: the lock excludes nobody;"
                " retry, and find out what is deleting it"
            )
        self._fd, self._ino = fd, ino

    def note(self, *, epoch: int, at: datetime) -> None:
        """Record who holds this term, for the refusal another process will
        print. Written under the lock, after the epoch is allocated."""
        if self._fd is None:
            raise EngineError(f"{self.path}: not held")
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
        """Re-prove leadership (S6b). Cheap by design -- one `stat`, against
        the `fsync` every real-domain append already pays."""
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
        """Idempotent. The file is NOT unlinked: unlinking is the one thing
        `check` exists to catch, and a lock file left behind excludes
        nobody -- the kernel released the lock when this fd closed, and the
        stale note is only ever read when a live holder refuses someone."""
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

    For a caller whose first act is not opening the log: `dsl41 run
    --detached` STARTS a supervisor and takes its lease, and a process that
    does that and is then refused has already acted on the estate it does
    not lead. The engine's own entry points acquire for themselves when no
    caller has."""
    # every entry down to the run root is a record: without the fsyncs a
    # power cut can lose the directory every later durable write lives in.
    # Unconditional, so a retry after a failed fsync repairs it rather than
    # skipping it (mkdir_durable's contract).
    mkdir_durable(str(run_root))
    lock = LeaderLock(run_root)
    lock.acquire()
    return lock


class Fence:
    """Every proof this process holds, re-proved together (period-model
    ss1.3).

    One appender, two things to prove: it leads this run root, and it still
    leads the lineage the anchor names. Delete or replace either directory
    and the incumbent stops on its next act -- DL-101's bargain, which does
    not claim to un-run what already happened and does turn a divergence
    into a recorded stop. Composing them here rather than at each call site
    is what stops a writer from proving one and forgetting the other; a
    root with no anchor holds a fence of one lock and behaves exactly as it
    did before."""

    def __init__(self, *locks: LeaderLock) -> None:
        self.locks = locks

    def check(self) -> None:
        for lock in self.locks:
            lock.check()

    def intact(self) -> bool:
        """The same proof as a boolean, for the one caller that must DECIDE
        rather than fail: a fence loss inside the boundary's reversible
        interval fail-stops instead of reopening admission (period-model
        ss7, DL-101's rule)."""
        try:
            self.check()
        except EngineError:
            return False
        return True

    def release(self) -> None:
        for lock in self.locks:
            lock.release()


def next_epoch(records: list[dict[str, Any]]) -> int:
    """ss1's monotone allocation, read from the ledger under the lock.

    One past the highest term the log records. A journal with no `leader`
    record was written before S6a and its inputs carry the inert epoch, so
    the first real term over it is 1 -- the same number a fresh run root
    gets, and for the same reason: no term has been held here yet."""
    terms = [int(r["epoch"]) for r in records if r.get("rec") == "leader" and "epoch" in r]
    return max(terms, default=0) + 1


def check_leader_eligibility(opening: dict[str, Any], *, catalog: CatalogIR) -> None:
    """ss7: eligibility requires an exact match on the log's two opening
    pins. Both checks live here rather than one here and one at the resume
    site -- they answer one question ("may this build lead this log?") and
    splitting them is how the second one goes missing.

    The catalog hash is recomputed under the recipe the log's own `segment`
    names (period-model ss1.1), never under the one this build happens to
    write: comparing across recipes is how a gate refuses an estate that
    did not change, which is the outage DL-100 named."""
    if opening.get("catalog_hash") != catalog_hash_for(opening, catalog):
        raise EngineError(
            "catalog hash mismatch: the estate changed since this journal was written;"
            " re-baseline explicitly with a fresh run (no silent semantic drift, ss7)"
        )
    pinned = opening.get("state_machine_version", _ASSUMED_VERSION)
    if pinned != STATE_MACHINE_VERSION:
        raise EngineError(
            f"state-machine version mismatch: this journal is v{pinned}, this build"
            f" derives v{STATE_MACHINE_VERSION}; a leader must derive identical"
            " revisions from identical inputs (concurrency-model ss7) -- re-baseline"
            " explicitly with a fresh run"
        )
