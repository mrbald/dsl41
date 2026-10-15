"""Pytest plugin: a test that leaves global process state changed fails (DL-355).

An autouse fixture reads every probe before each test and again after the
test's other function fixtures are torn down. Its teardown fails the test,
naming each probe that changed with the state before and after. No probe
waits or polls. Two read by writing: the wakeup fd is read by setting it and
put back, and outside Linux so is the umask.

Fixtures of a wider scope are set up before the detector and torn down after
it, so a module- or session-scoped fixture that leaves state changed is not
reported; none does today.

Restore fixtures put back state that a production path changes on purpose.
Each acts only when the state is that path's own change, and is torn down
before the detector checks:
- `_restore_recursion_limit` (conftest.py): the oracle raises the
  interpreter's recursion limit for the whole process (DL-306), and many
  tests build one. A test that sets the limit itself uses
  `own_recursion_limit`.
- `_restore_child_subreaper` (conftest.py): `Supervisor.run()` makes its
  process a Linux child subreaper, and some tests run a supervisor in-process
  (DL-354).
- `_restore_run_signal_handlers` (conftest.py): `dsl41 run` installs asyncio
  handlers for SIGINT, SIGTERM and SIGHUP, and closing its loop leaves the
  defaults, not the handlers it found; tests run it in-process, under coverage
  too.
- `_restore_signal_handlers` (test_runner_supervisor.py): `Supervisor.run()`
  installs its handlers before it binds, and a test that stops it at the bind
  leaves them behind.

Two probes count only what is new: a child process, or a non-daemon thread,
alive after the test and not before it. A test reaps its children and joins
its threads before it ends. A child or thread that ends on its own a moment
after the test is reported only when it is still there at the check. Daemon
threads are not probed: asyncio's child-watcher threads end a moment after
the wait they serve returns.
"""

from __future__ import annotations

import asyncio
import ctypes
import gc
import logging
import os
import signal
import sys
import threading
import warnings
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest

PR_GET_CHILD_SUBREAPER = 37


@dataclass(frozen=True)
class Probe:
    """One piece of global process state. `read` returns a comparable value.
    A population probe reads a frozenset, and only new members count."""

    name: str
    read: Callable[[], object]
    population: bool = False


def _libc_function(name: str) -> Callable[..., int] | None:
    try:
        function: Callable[..., int] = getattr(ctypes.CDLL(None, use_errno=True), name)
    except (OSError, AttributeError):
        return None
    return function


_PRCTL = _libc_function("prctl") if sys.platform.startswith("linux") else None
_LISTCHILDPIDS = _libc_function("proc_listchildpids") if sys.platform == "darwin" else None


def _subreaper() -> int | None:
    """Linux's child-subreaper flag; None elsewhere."""
    if _PRCTL is None:
        return None
    flag = ctypes.c_int(0)
    if _PRCTL(PR_GET_CHILD_SUBREAPER, ctypes.byref(flag), 0, 0, 0) != 0:
        return None
    return flag.value


def _signal_handlers() -> dict[str, object]:
    """Every signal's Python-level handler, and the wakeup fd. Reading the
    wakeup fd means setting it, so a value other than -1 is set back."""
    handlers: dict[str, object] = {}
    for signum in sorted(signal.valid_signals()):
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        handlers[name] = signal.getsignal(signum)
    fd = signal.set_wakeup_fd(-1)
    if fd != -1:
        signal.set_wakeup_fd(fd)
    handlers["wakeup fd"] = fd
    return handlers


def _environment() -> dict[str, str]:
    """`os.environ` without the variable pytest rewrites for each phase."""
    return {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}


def _umask() -> int:
    """Linux reads the umask from /proc; elsewhere reading it means setting it."""
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except OSError:
        pass
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def _cwd() -> str:
    try:
        return os.getcwd()
    except FileNotFoundError:
        return "<deleted>"


def _linux_children() -> frozenset[int]:
    """Each thread's children file, where the kernel has one
    (CONFIG_PROC_CHILDREN); else every process whose parent is this one."""
    pids: set[int] = set()
    if os.path.exists(f"/proc/self/task/{os.getpid()}/children"):
        for task in os.listdir("/proc/self/task"):
            try:
                with open(f"/proc/self/task/{task}/children", encoding="ascii") as children:
                    pids.update(int(pid) for pid in children.read().split())
            except OSError:  # the thread ended between the listing and the read
                pass
        return frozenset(pids)
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="ascii", errors="replace") as stat:
                fields = stat.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):  # the process ended between the listing and the read
            continue
        if int(fields[1]) == me:
            pids.add(int(entry))
    return frozenset(pids)


def _darwin_children(listchildpids: Callable[..., int]) -> frozenset[int]:
    buffer = (ctypes.c_int * 1024)()
    count = listchildpids(os.getpid(), buffer, ctypes.sizeof(buffer))
    return frozenset(pid for pid in buffer[: max(count, 0)] if pid > 0)


def child_pids() -> frozenset[int]:
    """This process's children, running or not yet reaped. Linux and macOS;
    elsewhere the probe sees none."""
    if sys.platform.startswith("linux"):
        return _linux_children()
    if _LISTCHILDPIDS is not None:
        return _darwin_children(_LISTCHILDPIDS)
    return frozenset()


def _threads() -> frozenset[threading.Thread]:
    main = threading.main_thread()
    return frozenset(
        t for t in threading.enumerate() if t is not main and not t.daemon and t.is_alive()
    )


class ProbeUnavailable(RuntimeError):
    """A probe's interface is gone; the probe fails rather than read nothing."""


def _loop_policy() -> object:
    """The event-loop policy. Python deprecates policies in 3.14 and plans to
    remove them; when the getter is gone, this probe needs a new read."""
    getter = getattr(asyncio, "get_event_loop_policy", None)
    if getter is None:
        raise ProbeUnavailable("asyncio.get_event_loop_policy is gone (DL-355)")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return getter()


def _current_loop() -> object:
    """The loop set as this thread's current loop, while it is open. The
    policy keeps it in a private slot; the public getters may create one."""
    local = getattr(_loop_policy(), "_local", None)
    if local is None or not hasattr(local, "_loop"):
        raise ProbeUnavailable("the event-loop policy keeps no current loop in `_local._loop`")
    loop = local._loop
    if isinstance(loop, asyncio.AbstractEventLoop) and not loop.is_closed():
        return loop
    return None


PROBES: tuple[Probe, ...] = (
    Probe("recursion limit", sys.getrecursionlimit),
    Probe("child subreaper", _subreaper),
    Probe("gc enabled", gc.isenabled),
    Probe("gc threshold", gc.get_threshold),
    Probe("signal handlers", _signal_handlers),
    Probe("environment", _environment),
    Probe("working directory", _cwd),
    Probe("umask", _umask),
    Probe("sys.path", lambda: list(sys.path)),
    Probe("child processes", child_pids, population=True),
    Probe("non-daemon threads", _threads, population=True),
    Probe("event-loop policy", _loop_policy),
    Probe("open current event loop", _current_loop),
    Probe("root logger handlers", lambda: list(logging.root.handlers)),
    Probe("root logger level", lambda: logging.root.level),
    Probe("logging disable level", lambda: logging.root.manager.disable),
)


def snapshot(probes: tuple[Probe, ...] = PROBES) -> dict[str, object]:
    return {probe.name: probe.read() for probe in probes}


def _mapping_change(old: dict[str, object], new: dict[str, object]) -> str:
    parts = []
    for key in sorted(old.keys() | new.keys()):
        if key not in new:
            parts.append(f"{key} removed (was {old[key]!r})")
        elif key not in old:
            parts.append(f"{key} added ({new[key]!r})")
        elif old[key] != new[key]:
            parts.append(f"{key}: before {old[key]!r}, after {new[key]!r}")
    return "; ".join(parts)


def _list_change(old: list[object], new: list[object]) -> str:
    added = list((Counter(new) - Counter(old)).elements())
    removed = list((Counter(old) - Counter(new)).elements())
    if not added and not removed:
        return f"reordered: before {old!r}, after {new!r}"
    return f"added {added!r}, removed {removed!r} (before {len(old)} entries, after {len(new)})"


def changes(
    before: dict[str, object], after: dict[str, object], probes: tuple[Probe, ...] = PROBES
) -> list[str]:
    """One line per probe that changed, naming the state before and after."""
    lines = []
    for probe in probes:
        old, new = before[probe.name], after[probe.name]
        if probe.population:
            assert isinstance(old, frozenset) and isinstance(new, frozenset)
            if added := new - old:
                shown = ", ".join(sorted(repr(member) for member in added))
                lines.append(f"{probe.name}: new {shown} (before: {len(old)} alive)")
        elif old != new:
            if isinstance(old, dict) and isinstance(new, dict):
                detail = _mapping_change(old, new)
            elif isinstance(old, list) and isinstance(new, list):
                detail = _list_change(old, new)
            else:
                detail = f"before {old!r}, after {new!r}"
            lines.append(f"{probe.name}: {detail}")
    return lines


def pytest_configure(config: pytest.Config) -> None:
    """The first read of the event-loop policy creates the default one. That
    is no test's change, so it happens here, before any test; a probe whose
    interface is gone stops the session here."""
    _current_loop()


@pytest.fixture(autouse=True)
def _leak_detector() -> Iterator[None]:
    before = snapshot()
    yield
    if leaked := changes(before, snapshot()):
        pytest.fail(
            "the test left global process state changed (DL-355):\n  " + "\n  ".join(leaked),
            pytrace=False,
        )
