"""The processor-time bound on a person's pattern (DL-514, `match_bound.py`).

A stop is asserted only for a pattern that would run for minutes, under a small limit; a pass
expected to finish runs under the jobs filter's limit, the smaller of DL-514's two. Wall-clock ceilings are loose: they hold that
a stop came, not how fast.
"""

from __future__ import annotations

import re
import signal
import threading
import time
from pathlib import Path

import pytest

from dsl41 import match_bound
from dsl41.match_bound import STOPPED, run_bounded

if not hasattr(signal, "setitimer"):  # pragma: no cover
    pytest.skip("the bound needs signal.setitimer", allow_module_level=True)

#: a ceiling for a stopped pass on a loaded machine; the stop itself comes in milliseconds
WALL_CEILING_S = 20.0
SMALL_LIMIT_S = 0.02
#: the jobs filter's limit (DL-514)
FILTER_LIMIT_S = 0.25

#: each shape against a text that makes it backtrack for minutes or longer
BACKTRACKING = [
    pytest.param(r"(a+)+$", "a" * 40 + "b", id="nested-plus"),
    pytest.param(r"(a|a)*$", "a" * 40 + "b", id="equal-alternatives"),
    pytest.param(r"(a|aa)+$", "a" * 80 + "b", id="overlapping-alternatives"),
    # each start before the a's tries every subset of them: 2**11 paths per start, so about
    # four minutes over five million starts
    pytest.param(r"(.*a){12}", "x" * 5_000_000 + "a" * 11, id="dot-star-repeat"),
    pytest.param(r"([a-z_]+)+\d$", "core_daily_load_customer_accounts_x", id="job-name"),
]


def _idle() -> bool:
    """Nothing armed: no deadline and a zero timer."""
    return match_bound._deadline is None and signal.getitimer(signal.ITIMER_VIRTUAL) == (0.0, 0.0)


def test_a_result_passes_through() -> None:
    assert run_bounded(lambda: 42, FILTER_LIMIT_S) == 42
    assert run_bounded(lambda: None, FILTER_LIMIT_S) is None
    assert _idle()


def test_an_error_in_the_work_propagates_and_leaves_nothing_armed() -> None:
    def fail() -> None:
        raise ValueError("work failed")

    with pytest.raises(ValueError, match="work failed"):
        run_bounded(fail, FILTER_LIMIT_S)
    assert _idle()


@pytest.mark.parametrize(("pattern", "text"), BACKTRACKING)
def test_each_backtracking_shape_is_stopped(pattern: str, text: str) -> None:
    compiled = re.compile(pattern, re.IGNORECASE)
    started = time.monotonic()
    assert run_bounded(lambda: compiled.search(text), SMALL_LIMIT_S) is STOPPED
    assert time.monotonic() - started < WALL_CEILING_S
    assert _idle()


def test_a_swallowed_stop_is_raised_again() -> None:
    """The timer repeats until the pass disarms it, so work that catches the first stop is
    stopped by the next delivery."""
    compiled = re.compile(r"(a+)+$")
    swallowed: list[str] = []

    def stubborn() -> object:
        try:
            compiled.search("a" * 40 + "b")
        except Exception as exc:  # noqa: BLE001 -- the work under test swallows the stop
            swallowed.append(type(exc).__name__)
        return compiled.search("a" * 40 + "b")

    assert run_bounded(stubborn, SMALL_LIMIT_S) is STOPPED
    assert swallowed == ["_Overrun"]
    assert _idle()


def test_the_handler_is_installed_once_and_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    installs: list[object] = []
    real_signal = signal.signal

    def counting(signum: int, handler: object) -> object:
        installs.append(handler)
        return real_signal(signum, handler)  # type: ignore[arg-type]

    monkeypatch.setattr(signal, "signal", counting)
    compiled = re.compile(r"(a+)+$")
    for _ in range(20):
        assert run_bounded(lambda: 1, FILTER_LIMIT_S) == 1
        assert run_bounded(lambda: compiled.search("a" * 40 + "b"), SMALL_LIMIT_S) is STOPPED
    assert installs == [match_bound._on_vtalrm]
    assert signal.getsignal(signal.SIGVTALRM) is match_bound._on_vtalrm


def test_a_pass_after_a_stop_is_not_stopped_by_a_stale_delivery() -> None:
    """A delivery outside a pass, or inside a pass that is not past its deadline, raises
    nothing."""
    compiled = re.compile(r"(a+)+$")
    assert run_bounded(lambda: compiled.search("a" * 40 + "b"), SMALL_LIMIT_S) is STOPPED
    signal.raise_signal(signal.SIGVTALRM)  # late, with no pass armed

    def stale_then_answer() -> str:
        signal.raise_signal(signal.SIGVTALRM)  # stale, inside a fresh pass
        return "answered"

    assert run_bounded(stale_then_answer, FILTER_LIMIT_S) == "answered"
    assert _idle()


def test_a_nested_pass_keeps_the_outer_limit() -> None:
    compiled = re.compile(r"(a+)+$")
    seen: list[object] = []

    def outer() -> object:
        deadline = match_bound._deadline
        seen.append(run_bounded(lambda: "inner", 60.0))
        # the inner pass neither moved the deadline nor disarmed the timer
        seen.append(match_bound._deadline == deadline)
        seen.append(signal.getitimer(signal.ITIMER_VIRTUAL)[1] > 0)
        return run_bounded(lambda: compiled.search("a" * 40 + "b"), 60.0)

    assert run_bounded(outer, SMALL_LIMIT_S) is STOPPED
    assert seen == ["inner", True, True]
    assert _idle()


def test_off_the_main_thread_the_work_runs_and_nothing_is_armed() -> None:
    seen: list[object] = []

    def work() -> str:
        seen.append(match_bound._deadline)
        seen.append(signal.getitimer(signal.ITIMER_VIRTUAL))
        return "ran"

    worker = threading.Thread(target=lambda: seen.append(run_bounded(work, FILTER_LIMIT_S)))
    worker.start()
    worker.join()
    assert seen == [None, (0.0, 0.0), "ran"]


def test_without_setitimer_the_work_runs_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    before = signal.getsignal(signal.SIGVTALRM)
    monkeypatch.delattr(signal, "setitimer")
    seen: list[object] = []

    def work() -> str:
        seen.append(match_bound._deadline)
        return "ran"

    assert run_bounded(work, FILTER_LIMIT_S) == "ran"
    assert seen == [None]
    assert signal.getsignal(signal.SIGVTALRM) is before


def test_an_honest_pattern_over_ten_thousand_lines_is_not_stopped() -> None:
    """Under the jobs filter's limit: a few milliseconds of processor time against 250."""
    lines = [
        f"2026-11-17T10:{i % 60:02d}:00 job core_load_{i} wrote {i * 7} rows" for i in range(10_000)
    ]
    compiled = re.compile(r"core_load_\d+ wrote \d+", re.IGNORECASE)
    shown = run_bounded(lambda: [line for line in lines if compiled.search(line)], FILTER_LIMIT_S)
    assert shown is not STOPPED
    assert len(shown) == 10_000


def test_a_stop_before_the_disarm_still_leaves_nothing_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop that surfaces before the disarm has run skips it; the stopped pass disarms again,
    so the deadline is clear, the timer reads zero and the next pass is bounded."""
    real_setitimer = signal.setitimer
    disarms: list[float] = []

    def stop_at_first_disarm(which: int, seconds: float, interval: float = 0.0) -> object:
        if seconds == 0:
            disarms.append(seconds)
            if len(disarms) == 1:
                raise match_bound._Overrun
        return real_setitimer(which, seconds, interval)

    monkeypatch.setattr(signal, "setitimer", stop_at_first_disarm)
    assert run_bounded(lambda: "done", FILTER_LIMIT_S) is STOPPED
    assert disarms == [0, 0]
    assert _idle()
    monkeypatch.setattr(signal, "setitimer", real_setitimer)
    compiled = re.compile(r"(a+)+$")
    assert run_bounded(lambda: compiled.search("a" * 40 + "b"), SMALL_LIMIT_S) is STOPPED
    assert _idle()


def test_only_match_bound_names_the_timer_or_its_signal() -> None:
    """Nothing else in the package arms `ITIMER_VIRTUAL` or handles `SIGVTALRM` (DL-514)."""
    package = Path(__file__).resolve().parents[1] / "src" / "dsl41"
    naming = sorted(
        path.name
        for path in package.rglob("*.py")
        if re.search(r"ITIMER_VIRTUAL|SIGVTALRM", path.read_text(encoding="utf-8"))
    )
    assert naming == ["match_bound.py"]
