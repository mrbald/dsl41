"""Run a person's pattern under a limit of the process's own processor time (DL-514).

A pass arms the interval timer `ITIMER_VIRTUAL`. The `SIGVTALRM` handler is installed at the
first armed pass and never removed or replaced. It raises a private exception only while a pass
is armed and the process's processor time is past that pass's deadline, so a late or stale
delivery raises nothing. The timer repeats until the pass disarms it, so a stop that a finalizer
swallowed is raised again. The regular expression matcher looks for a signal every 4096 steps,
which is what lets the stop interrupt a pattern that backtracks.

A pass inside a pass runs under the outer limit and leaves the outer pass armed. Off the main
thread, or where the platform has no such timer, the callable runs without a limit. The caller
names the limit; DL-514 sets one for the jobs filter and one for the log pager.
"""

from __future__ import annotations

import enum
import signal
import threading
import time
from collections.abc import Callable
from types import FrameType

#: seconds of processor time between repeated deliveries after the limit
_REPEAT_SECONDS = 0.01


class Stopped(enum.Enum):
    """The value a pass returns when its limit stopped it."""

    STOPPED = "stopped"


STOPPED = Stopped.STOPPED


class _Overrun(Exception):
    """Raised by the handler inside an armed pass that is past its deadline."""


#: the processor time at which the armed pass ends; None while no pass is armed
_deadline: float | None = None


def _on_vtalrm(_signum: int, _frame: FrameType | None) -> None:
    if _deadline is not None and time.process_time() >= _deadline:
        raise _Overrun


def _boundable() -> bool:
    return (
        threading.current_thread() is threading.main_thread()
        and hasattr(signal, "setitimer")
        and hasattr(signal, "ITIMER_VIRTUAL")
        and hasattr(signal, "SIGVTALRM")
    )


def run_bounded[T](work: Callable[[], T], seconds: float) -> T | Stopped:
    """`work()`, or STOPPED when it used more than `seconds` of the process's processor time.

    The catch surrounds the whole armed span, the disarm included. A result computed at the
    very limit may be reported as stopped."""
    global _deadline
    if _deadline is not None or not _boundable():
        return work()
    if signal.getsignal(signal.SIGVTALRM) is not _on_vtalrm:
        signal.signal(signal.SIGVTALRM, _on_vtalrm)
    try:
        try:
            _deadline = time.process_time() + seconds
            signal.setitimer(signal.ITIMER_VIRTUAL, seconds, _REPEAT_SECONDS)
            return work()
        finally:
            _deadline = None
            signal.setitimer(signal.ITIMER_VIRTUAL, 0)
    except _Overrun:
        # a stop can surface before the disarm above has run, so a stopped pass clears the
        # deadline and disarms again; a second stop cannot come sooner than one repeat interval
        _deadline = None
        signal.setitimer(signal.ITIMER_VIRTUAL, 0)
        return STOPPED
