"""supervisor_process as a statechart (docs/statechart-engine.md §16).

The chart is well formed and draws no warning. The generated completeness test
runs every witness configuration by every event and payload partition, and a
table written from supervisor-protocol §5, not from the rows, pins each cell.
Every event name the supervisor posts is in the chart's alphabet, and the
chart's own actions raise nothing. The fence of SPAWN, SIGNAL and SHUTDOWN is
the lease chart's guards, so the rule has one copy, and each refusal is
answered as before. A statechart error while the supervisor tears down stops
it with exit 7 and the report.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import statechart_completeness as sc

from dsl41 import runner_supervisor
from dsl41.machines import MACHINES
from dsl41.state_machine import (
    HITS_ENV,
    STATECHART_LOG_PREFIX,
    STRICT_ENV,
    alphabet,
    chart_warnings,
    well_formed,
)

CHART = runner_supervisor.SUPERVISOR_PROCESS
LEASE = runner_supervisor.SUPERVISOR_LEASE
INCARNATION = "a" * 32
TOKEN = 7
NOW = 1000.0
WALL = 1_700_000_000.0

#: each phase of the process as a configuration of SUPERVISOR_PROCESS
PROCESS: dict[str, frozenset[str]] = {
    "starting": frozenset({"process", "open", "starting"}),
    "bound": frozenset({"process", "open", "bound"}),
    "serving": frozenset({"process", "open", "up", "serving"}),
    "shutting_down": frozenset({"process", "open", "up", "shutting_down"}),
    "stopped": frozenset({"process", "ended", "stopped"}),
    "refused": frozenset({"process", "ended", "refused"}),
    "closed": frozenset({"process", "closed"}),
}
#: each phase of the lease as a configuration of SUPERVISOR_LEASE
LEASE_PHASES: dict[str, frozenset[str]] = {
    "free": frozenset({"lease", "unheld", "free"}),
    "expired": frozenset({"lease", "unheld", "expired"}),
    "live": frozenset({"lease", "held", "live"}),
    "orphaned": frozenset({"lease", "held", "orphaned"}),
}


def test_the_chart_is_well_formed_and_draws_no_warning() -> None:
    assert well_formed(CHART) == []
    assert chart_warnings(CHART) == []
    assert CHART.clock == "monotonic"
    assert CHART in MACHINES


def test_the_fence_is_the_lease_charts_guards() -> None:
    """One copy of the fence rule: the process chart registers the lease
    chart's callables, and the old check is gone. `stale` is built from them:
    this incarnation and not the credentials."""
    for name in ("foreign", "credentials"):
        assert CHART.guards[name] is LEASE.guards[name]
    assert "this_incarnation" not in CHART.guards
    assert not hasattr(runner_supervisor.Supervisor, "_check_token")
    assert not hasattr(runner_supervisor.Supervisor, "_move")


# ---- the abstraction (§13)


def _witness(phase: str, lease_phase: str = "free") -> sc.Builder:
    """The process in `phase` and the lease in `lease_phase`, in one executor, on
    a stand-in for the supervisor that holds what the guards read: the lease
    record, the incarnation and the executor. The token is 7; an expired
    deadline is the instant itself."""

    def build() -> tuple[Any, str]:
        lease = None
        if lease_phase != "free":
            deadline = NOW if lease_phase == "expired" else NOW + 60
            conn = None if lease_phase == "orphaned" else object()
            lease = runner_supervisor._Lease("h", TOKEN, deadline, "then", conn)
        executor = runner_supervisor.Executor()
        data = SimpleNamespace(lease=lease, incarnation=INCARNATION, _machines=executor)
        executor.restore("lease", LEASE, LEASE_PHASES[lease_phase], data)
        executor.restore("process", CHART, PROCESS[phase], data)
        return executor, "process"

    return build


_CREDENTIALS: dict[str, dict[str, Any]] = {
    "current": {"incarnation": INCARNATION, "token": TOKEN},
    "stale": {"incarnation": INCARNATION, "token": TOKEN + 1},
    "foreign": {"incarnation": "b" * 32, "token": TOKEN},
    "none": {"token": TOKEN},
}


def _partitions() -> dict[str, dict[str, Any]]:
    verbs = {
        verb: {
            label: runner_supervisor._Request({"cmd": verb, **fields})
            for label, fields in _CREDENTIALS.items()
        }
        for verb in ("SPAWN", "SIGNAL", "SHUTDOWN")
    }
    return {
        runner_supervisor.START: {
            fact: fact
            for fact in (runner_supervisor.LOCK_HELD, runner_supervisor.OWNER_LIVE, "free")
        },
        runner_supervisor.DRAINED: {
            fact: fact for fact in (runner_supervisor.REAPED, runner_supervisor.PAST_BOUND)
        },
        **verbs,
    }


WITNESSES: dict[str, sc.Builder] = {
    **{phase: _witness(phase) for phase in PROCESS if phase != "serving"},
    **{f"serving/{lease}": _witness("serving", lease) for lease in LEASE_PHASES},
}
ABSTRACTION = sc.Abstraction(witnesses=WITNESSES, partitions=_partitions())


def _row(number: str) -> str:
    return f"supervisor_process.{number}"


_IGNORED_ONCE_CLOSED = {
    runner_supervisor.START: "21",
    runner_supervisor.PUBLISH: "22",
    runner_supervisor.TERM: "23",
    runner_supervisor.DRAINED: "24",
    runner_supervisor.PASS_ENDED: "25",
    runner_supervisor.DEADMAN: "26",
    runner_supervisor.TEARDOWN: "27",
    "SPAWN": "28",
    "SIGNAL": "29",
    "SHUTDOWN": "30",
}


def _section_5(witness: str, event: str, label: str) -> str:
    """The cell as supervisor-protocol §5 states it, written without the rows.

    A start refuses when the lock is held or an owner answers, and binds
    otherwise; publishing serves. A mutating verb checks the incarnation
    first, then the token of an unexpired (live or orphaned) lease: SPAWN and
    SIGNAL then do their work while serving, and SHUTDOWN shuts down. A
    signal shuts down a serving process, runs the wait again after an
    errored SHUTDOWN, and does nothing once stopped; the end of that pass runs
    the wait again too. The wait's end stops the process, whichever way it
    ended. The deadman stops a serving process. Teardown closes, by the
    ordinary row from stopped or refused and by the abort row from anything
    else. A closed process takes nothing."""
    phase, _, lease = witness.partition("/")
    if phase == "closed":
        return f"ignored {_row(_IGNORED_ONCE_CLOSED[event])}"
    if event == runner_supervisor.START:
        return _row({"lock_held": "01", "owner_live": "02", "free": "03"}[label])
    if event in ("SPAWN", "SIGNAL", "SHUTDOWN"):
        if label in ("foreign", "none"):
            return "refused wrong_incarnation"
        if label == "stale" or lease not in ("live", "orphaned"):
            return "refused stale_token"
        return _row({"SPAWN": "13", "SIGNAL": "14", "SHUTDOWN": "05"}[event])
    if event == runner_supervisor.TERM:
        return _row("09" if phase == "stopped" else "06")
    if event == runner_supervisor.TEARDOWN:
        return _row("10" if phase in ("stopped", "refused") else "11")
    rows = {
        runner_supervisor.PUBLISH: "04",
        runner_supervisor.DRAINED: "07",
        runner_supervisor.PASS_ENDED: "12",
        runner_supervisor.DEADMAN: "08",
    }
    return _row(rows[event])


def test_the_completeness_table_is_section_5() -> None:
    table = sc.completeness_table(CHART, ABSTRACTION)
    expected = {cell: _section_5(*cell) for cell in table}
    assert table == expected
    assert {event for _, event, _ in table} == set(alphabet(CHART))
    reached = {(witness.partition("/")[0], event) for witness, event, _ in table}
    # each fact where it is posted; every event in closed; each verb while serving
    assert reached == {
        ("starting", "start"),
        ("bound", "publish"),
        ("serving", "term"),
        ("shutting_down", "term"),
        ("stopped", "term"),
        ("shutting_down", "drained"),
        ("shutting_down", "pass_ended"),
        ("serving", "deadman"),
        *((phase, "teardown") for phase in PROCESS),
        *(("closed", event) for event in _IGNORED_ONCE_CLOSED),
        *(("serving", verb) for verb in ("SPAWN", "SIGNAL", "SHUTDOWN")),
    }
    # ten witnesses (serving by four leases); per event, the reached cells by
    # partition: start 3+3, publish 1+1, term 4+1+1+1, drained 2+2, pass_ended
    # 1+1, deadman 4+1, teardown 10, each verb four leases by four
    # credentials, and each verb in closed by four credentials
    assert len(WITNESSES) == 10
    assert len(table) == 6 + 2 + 7 + 4 + 2 + 5 + 10 + 3 * 16 + 3 * 4


_OUT_OF_ORDER = {
    runner_supervisor.START: "serving",
    runner_supervisor.PUBLISH: "starting",
    runner_supervisor.TERM: "bound",
    runner_supervisor.DRAINED: "stopped",
    runner_supervisor.PASS_ENDED: "serving",
    runner_supervisor.DEADMAN: "shutting_down",
}


@pytest.mark.parametrize("event", sorted(_OUT_OF_ORDER))
def test_a_fact_posted_out_of_order_stops_the_supervisor(event: str) -> None:
    """The exclusions are not rows: a fact posted where the supervisor never
    posts it finds none and raises, which stops the supervisor with exit 7.
    teardown has no such cell: rows 10 and 11 take it in every state but
    closed."""
    executor, key = _witness(_OUT_OF_ORDER[event])()
    payload = "free" if event == runner_supervisor.START else None
    with pytest.raises(Exception) as caught:
        executor.dispatch(key, runner_supervisor.Event(event, payload))
    assert sc.is_statechart_error(caught.value)
    assert type(caught.value).__name__ == "UnhandledEvent"


@pytest.mark.parametrize("verb", ["SPAWN", "SIGNAL", "SHUTDOWN"])
@pytest.mark.parametrize("phase", ["starting", "bound", "shutting_down", "stopped", "refused"])
def test_a_credentialed_verb_outside_serving_stops_the_supervisor(phase: str, verb: str) -> None:
    """The fence rows sit on `serving`. A verb posted anywhere else, with this
    incarnation and the current token of a live lease, finds no row: it raises
    `UnhandledEvent`, not a `stale_token` answer that would tell the holder its
    own token is stale. In `closed` the root's ignored row takes it."""
    executor, key = _witness(phase, "live")()
    request = runner_supervisor._Request({"cmd": verb, "incarnation": INCARNATION, "token": TOKEN})
    with pytest.raises(Exception) as caught:
        executor.dispatch(key, runner_supervisor.Event(verb, request))
    assert sc.is_statechart_error(caught.value)
    assert type(caught.value).__name__ == "UnhandledEvent"


@pytest.mark.parametrize(("verb", "row"), [("SPAWN", "28"), ("SIGNAL", "29"), ("SHUTDOWN", "30")])
def test_a_verb_in_closed_is_ignored_by_the_root(verb: str, row: str) -> None:
    executor, key = _witness("closed", "live")()
    request = runner_supervisor._Request({"cmd": verb, "incarnation": INCARNATION, "token": TOKEN})
    (outcome,) = executor.dispatch(key, runner_supervisor.Event(verb, request))
    assert outcome.rows == (_row(row),)
    assert executor.active(key) == PROCESS["closed"]


# ---- the alphabet (DL-325)


def test_every_event_the_supervisor_posts_is_in_the_alphabet(
    short_root: Path, queued_supervisor: Any, monkeypatch: pytest.MonkeyPatch, stand: Any
) -> None:
    """The posters: `_bind` (start, publish), the dispatch belt for each
    mutating verb, the loop (term, pass_ended, the sampled deadman), the
    shutdown's wait (drained) and teardown. A whole in-process run that the
    deadman ends, a refused start, and each verb and loop event on a serving
    supervisor post every name. No action raises an event: every post is one
    microstep at depth 0, and the actions raise nothing."""
    posted: list[str] = []
    dispatch = runner_supervisor.Executor.dispatch

    def spy(self: Any, key: Any, event: Any) -> Any:
        outcomes = dispatch(self, key, event)
        if key == "process":
            assert [o.depth for o in outcomes] == [0], outcomes
            posted.append(event.name)
        return outcomes

    monkeypatch.setattr(runner_supervisor.Executor, "dispatch", spy)
    for name in ("_install_signals", "_set_subreaper", "_reap"):
        monkeypatch.setattr(runner_supervisor.Supervisor, name, lambda self: None)

    root = short_root / "deadman"
    root.mkdir()
    ended = runner_supervisor.Supervisor(str(root), deadman_s=1e-9)
    assert ended.run() == 0 and ended.state == "closed"
    held = runner_supervisor.flock_exclusive(root / "supervisor.lock")
    try:
        refused = runner_supervisor.Supervisor(str(root))
        with pytest.raises(SystemExit):
            refused.run()
        assert refused.state == "closed"
    finally:
        runner_supervisor.os.close(held)

    sup, connect = queued_supervisor
    conn, _peer = connect()
    stale = {"incarnation": sup.incarnation, "token": 99}
    for verb in ("SPAWN", "SIGNAL", "SHUTDOWN"):
        request = {"v": 1, "cmd": verb, "spec": {}, **stale}
        sup._dispatch(conn, json.dumps(request).encode("utf-8"))
    stand(sup, "shutting_down")
    sup._orderly_shutdown(runner_supervisor.PASS_ENDED)  # 12, then the wait
    sup._orderly_shutdown(runner_supervisor.TERM)  # 09
    assert sup.state == "stopped"
    assert set(posted) == set(alphabet(CHART))
    ctx = runner_supervisor.Context("process", runner_supervisor.Event("x"), frozenset(), None)
    for action in CHART.actions.values():
        effects = action(ctx)
        assert effects is not None and effects.send == () and effects.then == ""


# ---- one fence


def _clocked(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    clock = SimpleNamespace(now=NOW)
    clock.monotonic = lambda: clock.now
    clock.time = lambda: WALL
    clock.sleep = lambda _seconds: None
    monkeypatch.setattr(runner_supervisor, "time", clock)
    return clock


class _Fence:
    """A serving supervisor holding a lease of ten seconds, a clock the test
    moves, and the process rows each ask takes. Each case builds its own, so a
    verb that ends the process (SHUTDOWN) ends only its case."""

    def __init__(self, queued_supervisor: Any, monkeypatch: pytest.MonkeyPatch, verb: str) -> None:
        self.clock = _clocked(monkeypatch)
        self.sup, connect = queued_supervisor
        monkeypatch.setattr(self.sup, "_reap", lambda: None)
        self.conn, _peer = connect()
        self.verb = verb
        self.token = self.sup._h_acquire(self.conn, {"controller_id": "c", "ttl_s": 10})["token"]
        self.rows: list[str] = []
        dispatch = self.sup._machines.dispatch

        def spy(key: Any, event: Any) -> Any:
            outcomes = dispatch(key, event)
            if key == "process":
                self.rows.extend(row for o in outcomes for row in o.rows)
            return outcomes

        self.sup._machines.dispatch = spy
        first = {"SPAWN": 15, "SIGNAL": 17, "SHUTDOWN": 19}[verb]
        self.wrong_row = _row(f"{first:02d}")
        self.stale_row = _row(f"{first + 1:02d}")
        self.passing_row = _row({"SPAWN": "13", "SIGNAL": "14", "SHUTDOWN": "05"}[verb])

    def ask(self, **fields: Any) -> dict[str, Any]:
        request = {
            "v": 1,
            "cmd": self.verb,
            "spec": {},
            "run_id": "r",
            "sig": "TERM",
            **fields,
        }
        answer: dict[str, Any] = self.sup._answer(self.conn, json.dumps(request).encode("utf-8"))
        return answer

    def current(self) -> dict[str, Any]:
        return self.ask(incarnation=self.sup.incarnation, token=self.token)

    @property
    def wrong(self) -> dict[str, Any]:
        return {"ok": False, "error": "wrong_incarnation", "incarnation": self.sup.incarnation}


_STALE = {"ok": False, "error": "stale_token"}
_FENCED_VERBS = ["SPAWN", "SIGNAL", "SHUTDOWN"]


@pytest.mark.parametrize("verb", _FENCED_VERBS)
def test_one_fence_refuses_each_mutating_verb_as_before(
    queued_supervisor: Any, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """A foreign incarnation and a missing one are refused wrong_incarnation
    with the current incarnation, checked first as `_check_token` checked
    them. A stale token, and a boolean where the integer goes (DL-151), are
    refused stale_token. Each refusal takes the verb's row and leaves the
    process serving."""
    fence = _Fence(queued_supervisor, monkeypatch, verb)
    assert fence.ask(incarnation="b" * 32, token=fence.token) == fence.wrong
    assert fence.ask(token=fence.token) == fence.wrong
    assert fence.ask(incarnation=fence.sup.incarnation, token=fence.token + 1) == _STALE
    assert fence.ask(incarnation=fence.sup.incarnation, token=True) == _STALE
    assert fence.sup.state == "serving"
    assert fence.rows == [fence.wrong_row] * 2 + [fence.stale_row] * 2


@pytest.mark.parametrize("verb", _FENCED_VERBS)
def test_one_fence_refuses_the_current_token_at_its_deadline_instant(
    queued_supervisor: Any, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """The deadline instant is expired, as `_lease_active` read it: the
    supervisor posts the due expiry before the verb, and `credentials` reads
    `held`. The verb is refused stale_token on its own row, and RENEW at the
    same instant meets the same guard. For SHUTDOWN the process stays serving:
    a skipped expiry sample would let the expired holder shut it down, and the
    answer and the row below would differ."""
    fence = _Fence(queued_supervisor, monkeypatch, verb)
    fence.clock.now = NOW + 10
    assert fence.current() == _STALE
    renew = {"v": 1, "cmd": "RENEW", "incarnation": fence.sup.incarnation, "token": fence.token}
    assert fence.sup._answer(fence.conn, json.dumps(renew).encode("utf-8")) == _STALE
    assert fence.sup.state == "serving"
    assert fence.rows == [fence.stale_row]


@pytest.mark.parametrize("verb", _FENCED_VERBS)
def test_one_fence_passes_the_current_token_just_before_its_deadline(
    queued_supervisor: Any, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """A microsecond before the deadline the same credentials pass the fence,
    and the verb's own checks answer. SHUTDOWN takes row 05 and its wait ends
    with row 07, which leaves the process stopped."""
    fence = _Fence(queued_supervisor, monkeypatch, verb)
    fence.clock.now = NOW + 10 - 1e-6
    passed = fence.current()
    assert passed.get("error") == {"SPAWN": "bad_spec", "SIGNAL": "unknown_run"}.get(verb)
    if verb == "SHUTDOWN":
        assert fence.sup.state == "stopped"
        assert fence.rows == [fence.passing_row, _row("07")]
    else:
        assert fence.sup.state == "serving"
        assert fence.rows == [fence.passing_row]


# ---- a statechart error in teardown


def test_a_statechart_error_in_teardown_exits_7_with_the_report(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A start that loses the lock refuses with exit 1, then tears down. With a
    chart that lacks row 10, the teardown post finds no row: the statechart
    error replaces the refusal, and main prints the report under one prefix
    and exits 7, not 1 and not a traceback. Teardown stops at the post, so
    the process exit releases the rest. The run without the gap is the
    non-triggering case: exit 1. The gapped chart records its hits into a
    private directory, so its forbidden guard outcome stays out of the gate."""
    made: list[Any] = []

    class Recorded(runner_supervisor.Supervisor):
        def __init__(self, run_root: str, *, deadman_s: float | None = None) -> None:
            super().__init__(run_root, deadman_s=deadman_s)
            made.append(self)

    monkeypatch.setattr(runner_supervisor, "Supervisor", Recorded)
    for name in ("_install_signals", "_set_subreaper"):
        monkeypatch.setattr(Recorded, name, lambda self: None)
    held = runner_supervisor.flock_exclusive(short_root / "supervisor.lock")
    try:
        assert runner_supervisor.main(["--run-root", str(short_root)]) == 1
        assert made[-1].state == "closed"
        directory = tmp_path / "hits"
        directory.mkdir()
        monkeypatch.setenv(HITS_ENV, str(directory))
        monkeypatch.delenv(STRICT_ENV, raising=False)
        rows = tuple(row for row in CHART.rows if row.id != _row("10"))
        monkeypatch.setattr(runner_supervisor, "SUPERVISOR_PROCESS", replace(CHART, rows=rows))
        capsys.readouterr()
        assert runner_supervisor.main(["--run-root", str(short_root)]) == 7
    finally:
        runner_supervisor.os.close(held)
    stopped = made[-1]
    assert stopped.state == "refused"  # the teardown post took nothing
    for fd in (stopped._chld_r, stopped._chld_w):
        runner_supervisor.os.close(fd)
    stopped._sel.close()
    report = [line for line in capsys.readouterr().err.splitlines() if line]
    assert report[0] == (
        f"{STATECHART_LOG_PREFIX}: supervisor_process 'process': no row and no deferral"
        " for teardown in ['ended', 'process', 'refused']"
    )
    assert all(line.startswith(STATECHART_LOG_PREFIX) for line in report)
    assert f"{STATECHART_LOG_PREFIX}: machine: supervisor_process" in report
    assert "another supervisor owns this root" not in "\n".join(report)
