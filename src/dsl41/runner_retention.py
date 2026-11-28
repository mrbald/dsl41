"""Retention of the run root's files (DL-557): `runs/`, `logs/` and `plans/` follow the store.

An entry is removed when it is older, by a day, than the oldest row the live store keeps. A
run is `runs/<job>.<run>/` with everything in it plus `logs/<job>.<run>.out` and `.err`, judged
together by the newest mtime among them. A plan is `plans/<token>/`, judged by its `plan.json`,
or by the directory when it has none. A name that does not parse as the engine's, and an
entry that is a symbolic link, is left alone. Never removed: a run an open record names, the
run of each job's last dispatch, the staged read's plan, and every plan while a preparation or
an update is in flight (DL-569). With `[engine] expired_dir` set, an expired entry is moved
there, keeping its place and name, instead of removed; a move never replaces a name already
there.

A round lists the three directories on a thread of its own and reports the old entries to the
engine's loop. The loop removes them in bounded steps, each against the horizon it reads then.
The module imports nothing from the engine; `runner.py` wires it.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import stat
import threading
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from dsl41.reload_plan import PLAN_JSON
from dsl41.runner_journal import OpenTable

RUNS: Final = "runs"
LOGS: Final = "logs"
PLANS: Final = "plans"
#: the two streams a run's default output goes to (`runner_adapters.default_log_paths`)
LOG_SUFFIXES: Final = (".out", ".err")
#: how much older than the live store's first row an entry must be (DL-557)
MARGIN: Final = timedelta(hours=24)
#: a round begins at the first snapshot at least this long after the last round began
EVERY: Final = timedelta(hours=24)
#: the directory entries the worker lists between two looks at its stop flag
PAGE: Final = 1000
#: the entries one owed step examines, and so removes, at most
STEP: Final = 100

Kind = Literal["run", "plan"]


# ---- names


def run_name(name: str) -> tuple[str, int] | None:
    """`<job>.<run>` read from the right: the job and the run number, which is written in
    plain decimal. None for any other name."""
    job, dot, run = name.rpartition(".")
    if not dot or not job or not (run.isascii() and run.isdigit()) or str(int(run)) != run:
        return None
    return job, int(run)


def log_run(name: str) -> str | None:
    """The run a default log names, `<job>.<run>` of `<job>.<run>.out` or `.err`; None for
    any other name."""
    for suffix in LOG_SUFFIXES:
        if name.endswith(suffix):
            run = name.removesuffix(suffix)
            return run if run_name(run) is not None else None
    return None


def is_token(name: str) -> bool:
    """Whether `name` is a plan token: 64 lowercase hex digits (`reload_diff.plan_token`)."""
    return len(name) == 64 and all(c in "0123456789abcdef" for c in name)


# ---- the rule


def horizon(began: datetime | None, now: datetime | None) -> float | None:
    """The instant, in seconds since the epoch, an entry must be older than: the earlier of
    the first row's stamp when the round began and now, less `MARGIN`. Row stamps are not
    monotone in seq, so the earlier of the two is taken. None when the store held no row then
    or holds none now: nothing is removed."""
    if began is None or now is None:
        return None
    return (min(began, now) - MARGIN).replace(tzinfo=UTC).timestamp()


def kept_runs(open: OpenTable) -> frozenset[str]:
    """The runs no round removes, as `<job>.<run>`: each a pending start, an open attempt or
    an unfinished follow-up names, and each job's last dispatch."""
    runs = [(p.job, p.run_number) for p in open.pending.values()]
    runs += [(a.job, a.run_number) for a in open.attempts.values()]
    runs += [(p.job, p.run_number) for p in open.plans.values()]
    runs += [(job, int(body["run_number"])) for job, body in open.dispatches.items()]
    return frozenset(f"{job}.{run}" for job, run in runs)


@dataclass(frozen=True, slots=True)
class Entry:
    """A run, named `<job>.<run>`, or a plan, named by its token."""

    kind: Kind
    name: str

    def paths(self, root: Path) -> tuple[Path, ...]:
        """The entry's files, in the order they go: a run's logs before its directory, so a
        run directory moved out has its output beside it already."""
        if self.kind == "plan":
            return (root / PLANS / self.name,)
        return (*(root / LOGS / f"{self.name}{s}" for s in LOG_SUFFIXES), root / RUNS / self.name)

    def linked(self, root: Path) -> bool:
        """Whether one of the entry's files is a symbolic link: then the entry is not the
        engine's, and nothing of it is judged, removed or moved."""
        return any(os.path.islink(path) for path in self.paths(root))


def tree_mtime(path: Path) -> float | None:
    """The newest mtime of `path` and everything under it, links not followed; None when it
    does not exist. Raises `OSError` for one that cannot be read."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    newest = info.st_mtime
    if stat.S_ISDIR(info.st_mode):
        with os.scandir(path) as children:
            for child in children:
                found = tree_mtime(Path(child.path))
                newest = newest if found is None else max(newest, found)
    return newest


def plan_mtime(path: Path) -> float | None:
    """A plan's age: its `plan.json`'s mtime, or the directory's when it has none; one stat,
    never every captured part. None when the plan does not exist."""
    try:
        return os.lstat(path / PLAN_JSON).st_mtime
    except (FileNotFoundError, NotADirectoryError):
        pass
    try:
        return os.lstat(path).st_mtime
    except FileNotFoundError:
        return None


def entry_mtime(root: Path, entry: Entry) -> float | None:
    """The newest mtime among the entry's files; None when none of them exists."""
    if entry.kind == "plan":
        return plan_mtime(root / PLANS / entry.name)
    found = [tree_mtime(path) for path in entry.paths(root)]
    return max((m for m in found if m is not None), default=None)


# ---- the listing (the worker's thread)


class Stopped(Exception):
    """The round was asked to stop before its listing reached the end."""


@dataclass
class Listing:
    """What one round found: the entries older than its horizon, in the order listed, and the
    entries it could not read, with the first one's error."""

    old: deque[Entry] = field(default_factory=deque)
    errors: int = 0
    error: str | None = None

    def failed(self, path: Path, exc: OSError) -> None:
        self.errors += 1
        self.error = self.error or f"{path}: {exc}"


def _names(directory: Path, going_on: Callable[[], bool], listing: Listing) -> Iterator[str]:
    """The names in `directory` that are not symbolic links, read in pages of `PAGE` with a
    look at the stop flag between two pages. A directory that does not exist has none; one
    that cannot be opened or read on is an error, and the listing goes on without it."""
    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return
    except OSError as exc:
        listing.failed(directory, exc)
        return
    with entries:
        n = 0
        while True:
            try:
                entry = next(entries, None)
                linked = entry is not None and entry.is_symlink()
            except OSError as exc:
                listing.failed(directory, exc)
                return
            if entry is None:
                return
            if not linked:
                yield entry.name
            n += 1
            if n % PAGE == 0 and not going_on():
                raise Stopped


def list_old(root: Path, before: float, going_on: Callable[[], bool]) -> Listing:
    """List `runs/`, `logs/` and `plans/` to the end, past entries that cannot be read, and
    answer the entries whose newest mtime is before `before`. A run's files are grouped by
    name over the whole listing, so a fresh log keeps an old run directory, and an entry with
    a part that cannot be read is not reported. Raises `Stopped` when `going_on` says no."""
    listing = Listing()
    newest: dict[str, float | None] = {}

    def note(run: str, path: Path) -> None:
        try:
            found = tree_mtime(path)
        except OSError as exc:
            listing.failed(path, exc)
            newest[run] = None
            return
        if found is None or (run in newest and newest[run] is None):
            return  # gone meanwhile, or another part of the run could not be read
        held = newest.get(run)
        newest[run] = found if held is None else max(held, found)

    for name in _names(root / RUNS, going_on, listing):
        if run_name(name) is not None:
            note(name, root / RUNS / name)
    for name in _names(root / LOGS, going_on, listing):
        run = log_run(name)
        if run is not None:
            note(run, root / LOGS / name)
    listing.old.extend(
        Entry("run", run) for run, found in newest.items() if found is not None and found < before
    )
    for name in _names(root / PLANS, going_on, listing):
        if not is_token(name):
            continue
        try:
            found = plan_mtime(root / PLANS / name)
        except OSError as exc:
            listing.failed(root / PLANS / name, exc)
            continue
        if found is not None and found < before:
            listing.old.append(Entry("plan", name))
    return listing


class RetentionWorker:
    """One round's listing on a thread of its own (DL-557). It opens no store and writes
    nothing; it hands its listing to the loop with `call_soon_threadsafe` and ends. A round a
    day leaves no idle thread behind. A worker asked to stop before its report reports
    nothing."""

    def __init__(
        self, root: Path, before: float, number: int, report: Callable[[int, Listing], None]
    ) -> None:
        self.root = root
        self.before = before
        self.number = number
        self._report = report
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._main, name="dsl41-retention", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to end at its next page, or before its report if the listing is
        done; it reports nothing then."""
        self._stopping.set()

    def join(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the thread to end; True when it did or never ran."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _main(self) -> None:
        try:
            listing = list_old(self.root, self.before, lambda: not self._stopping.is_set())
        except Stopped:
            return
        if self._stopping.is_set():
            return  # asked to stop after the last page
        assert self._loop is not None  # `start` set it before the thread began
        with contextlib.suppress(RuntimeError):  # a closed loop has nobody to tell
            self._loop.call_soon_threadsafe(self._report, self.number, listing)


# ---- the steps (the engine's loop)


@dataclass
class Round:
    """One round as the loop holds it: its number, the first row's stamp when it began, the
    entries still owed once the listing came, and what its steps did."""

    number: int
    first_at: datetime | None
    owed: deque[Entry] = field(default_factory=deque)
    #: what went, by `runs`, `logs` and `plans`
    done: dict[str, int] = field(default_factory=lambda: {RUNS: 0, LOGS: 0, PLANS: 0})
    errors: int = 0
    error: str | None = None
    #: set once the round's line is said, or would have been
    ended: bool = False

    def listed(self, listing: Listing) -> None:
        """Take the round's one listing; its entries are handed over, not copied."""
        self.owed = listing.old
        self.errors += listing.errors
        self.error = self.error or listing.error

    def failed(self, path: Path, exc: OSError) -> None:
        self.errors += 1
        self.error = self.error or f"{path}: {exc}"

    def summary(self, expired: str | None) -> str | None:
        """The round's one line on standard error, or None when it removed nothing and met no
        error."""
        if not any(self.done.values()) and not self.errors:
            return None
        counts = " ".join(f"{kind}={n}" for kind, n in self.done.items())
        line = f"dsl41: retention {'removed' if expired is None else 'moved'} {counts}"
        if expired is not None:
            line += f" to {expired}"
        if self.errors:
            line += f"; errors={self.errors}, the first: {self.error}"
        return line + " (DL-557)"


def _delete(path: Path) -> None:
    """Remove a file, a link or a whole directory; links are not followed."""
    if stat.S_ISDIR(os.lstat(path).st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _target(path: Path, expired: Path) -> Path:
    """The same place and name under `expired`: `runs`, `logs` or `plans`, then the name."""
    return expired / path.parent.name / path.name


def _move(path: Path, expired: Path) -> None:
    """Move `path` to its place under `expired`, making `runs`, `logs` or `plans` there when
    missing; `expired` itself must exist. Never a copy, never a replace: a file is linked
    there and then unlinked here, and `link` refuses a name that is there; a directory is
    renamed, which cannot replace a directory that holds anything."""
    target = _target(path, expired)
    target.parent.mkdir(exist_ok=True)
    if stat.S_ISDIR(os.lstat(path).st_mode):
        os.rename(path, target)
    else:
        os.link(path, target)
        os.unlink(path)


def discard(root: Path, entry: Entry, expired: Path | None, round: Round) -> None:
    """Remove the entry's files, or move them under `expired`, in `Entry.paths` order. Every
    destination name of the entry is checked first, whether or not this entry has that part:
    when one is taken, nothing moves. A failure partway, such as a rename across file
    systems, leaves what moved where it is and the rest here, and is counted; a later round
    moves the rest once the archiver has taken what moved."""
    if expired is not None:
        taken = next((p for p in entry.paths(root) if os.path.lexists(_target(p, expired))), None)
        if taken is not None:
            target = str(_target(taken, expired))
            round.failed(taken, FileExistsError(errno.EEXIST, "already there", target))
            return
    for path in entry.paths(root):
        if not os.path.lexists(path):
            continue
        try:
            if expired is None:
                _delete(path)
            else:
                _move(path, expired)
        except OSError as exc:
            round.failed(path, exc)
            return
        round.done[path.parent.name] += 1


def step(
    root: Path,
    round: Round,
    *,
    before: float | None,
    runs: frozenset[str],
    plans: bool,
    expired: Path | None,
    staged: str | None = None,
) -> None:
    """One owed step: at most `STEP` entries off the round's list, and at most one plan
    removed, since a plan holds every captured part. An entry a kept run names, any plan
    while `plans` is True, the staged read's plan (`staged`, DL-569), an entry that is now a
    link, and an entry not older than `before` now are dropped; each other is removed or
    moved. With no `before` every entry is dropped."""
    plan_taken = False
    for _ in range(min(STEP, len(round.owed))):
        entry = round.owed.popleft()
        if before is None or (
            entry.name in runs if entry.kind == "run" else plans or entry.name == staged
        ):
            continue
        if entry.linked(root):
            continue
        try:
            found = entry_mtime(root, entry)
        except OSError as exc:
            round.failed(root / (RUNS if entry.kind == "run" else PLANS) / entry.name, exc)
            continue
        if found is None or found >= before:
            continue
        if entry.kind == "plan":
            if plan_taken:
                round.owed.appendleft(entry)  # the next step takes it
                return
            plan_taken = True
        discard(root, entry, expired, round)


__all__ = [
    "EVERY",
    "LOGS",
    "MARGIN",
    "PAGE",
    "PLANS",
    "RUNS",
    "STEP",
    "Entry",
    "Listing",
    "RetentionWorker",
    "Round",
    "Stopped",
    "discard",
    "entry_mtime",
    "horizon",
    "is_token",
    "kept_runs",
    "list_old",
    "log_run",
    "plan_mtime",
    "run_name",
    "step",
    "tree_mtime",
]
