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
    "draining": frozenset({"process", "open", "up", "draining"}),
    "shutting_down": frozenset({"process", "open", "up", "shutting_down"}),
    "stopped": frozenset({"process", "ended", "stopped"}),
    "refused": frozenset({"process", "ended", "refused"}),
    "closed": frozenset({"process", "closed"}),
    "maintenance_checking": frozenset({"process", "open", "up", "maintenance_checking"}),
    "maintenance_uncertain": frozenset({"process", "open", "up", "maintenance_uncertain"}),
    **{
        phase: frozenset({"process", "open", "up", "maintaining", phase})
        for phase in ("maintenance_stopping", "maintenance_completing")
    },
    "maintenance_completed": frozenset({"process", "ended", "maintenance_completed"}),
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
        for verb in ("SPAWN", "SIGNAL", "SHUTDOWN", "MAINTENANCE_STOP")
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
    **{phase: _witness(phase) for phase in PROCESS if phase not in ("serving", "draining")},
    **{
        f"{phase}/{lease}": _witness(phase, lease)
        for phase in ("serving", "draining")
        for lease in LEASE_PHASES
    },
}
ABSTRACTION = sc.Abstraction(witnesses=WITNESSES, partitions=_partitions())


def _row(number: str) -> str:
    return f"supervisor_process.{number}"


#: the teardown row of each phase but closed, by what the phase owns
_TEARDOWN = {
    "serving": "31",
    "draining": "31",
    "shutting_down": "31",
    "stopped": "32",
    "bound": "33",
    "starting": "34",
    "refused": "35",
    "maintenance_checking": "31",
    "maintenance_stopping": "31",
    "maintenance_uncertain": "31",
    "maintenance_completing": "31",
    "maintenance_completed": "47",
}

#: every event, each of which the final `closed` ignores (statechart-engine §10)
_EVENTS = (
    runner_supervisor.START,
    runner_supervisor.PUBLISH,
    runner_supervisor.TERM,
    runner_supervisor.DRAIN_ENDED,
    runner_supervisor.DRAINED,
    runner_supervisor.PASS_ENDED,
    runner_supervisor.DEADMAN,
    runner_supervisor.TEARDOWN,
    "SPAWN",
    "SIGNAL",
    "SHUTDOWN",
    "MAINTENANCE_STOP",
    runner_supervisor.MAINTENANCE_REFUSED,
    runner_supervisor.MAINTENANCE_STOPPING,
    runner_supervisor.MAINTENANCE_IDLE,
    runner_supervisor.MAINTENANCE_UNCERTAIN,
    runner_supervisor.MAINTENANCE_FINISHED,
    runner_supervisor.MAINTENANCE_PASS,
)


def _section_5(witness: str, event: str, label: str) -> str:
    """The cell as supervisor-protocol §5 states it, written without the rows.

    A start refuses when the lock is held or an owner answers, and binds
    otherwise; publishing serves. A mutating verb checks the incarnation
    first, then the token of an unexpired (live or orphaned) lease: SPAWN and
    SIGNAL then do their work while serving, and SHUTDOWN shuts down. A
    signal starts the drain of a serving process, runs the wait again after an
    errored SHUTDOWN, and does nothing once stopped; the end of that pass runs
    the wait again too. A drain refuses SPAWN, SHUTDOWN and MAINTENANCE_STOP
    `draining` and fences SIGNAL as serving does; a second signal or the
    drain's end runs the wait, and the deadman stops it (DL-416). The wait's
    end stops the process, whichever way it ended. The deadman stops a serving
    process. Teardown closes, by one row per class of what the state owns: the
    published socket from serving, draining, shutting_down and stopped, the
    private one from bound, and nothing from starting and refused. A closed
    process takes nothing."""
    phase, _, lease = witness.partition("/")
    if phase == "closed":
        return "ignored in closed"
    if event == runner_supervisor.START:
        return _row({"lock_held": "01", "owner_live": "02", "free": "03"}[label])
    if event in ("SPAWN", "SIGNAL", "SHUTDOWN", "MAINTENANCE_STOP"):
        if phase == "maintenance_uncertain":
            return "refused maintenance_uncertain"
        if phase in ("maintenance_stopping", "maintenance_completing"):
            return "refused maintenance_in_progress"
        if phase == "draining" and event != "SIGNAL":
            return "refused draining"
        if label in ("foreign", "none"):
            return "refused wrong_incarnation"
        if label == "stale" or lease not in ("live", "orphaned"):
            return "refused stale_token"
        if phase == "draining":
            return _row("67")
        return _row(
            {"SPAWN": "13", "SIGNAL": "14", "SHUTDOWN": "05", "MAINTENANCE_STOP": "37"}[event]
        )
    if phase == "draining":
        return _row(
            {
                runner_supervisor.TERM: "61",
                runner_supervisor.DRAIN_ENDED: "62",
                runner_supervisor.DEADMAN: "63",
                runner_supervisor.TEARDOWN: "31",
            }[event]
        )
    if event == runner_supervisor.TERM:
        return _row(
            {
                "stopped": "09",
                "shutting_down": "36",
                "maintenance_uncertain": "48",
                "maintenance_completed": "50",
            }.get(phase, "06")
        )
    if event == runner_supervisor.TEARDOWN:
        return _row(_TEARDOWN[phase])
    maintenance = {
        ("maintenance_checking", "maintenance_refused"): "40",
        ("maintenance_checking", "maintenance_stopping"): "41",
        ("maintenance_checking", "maintenance_idle"): "42",
        ("maintenance_stopping", "maintenance_idle"): "43",
        ("maintenance_stopping", "maintenance_uncertain"): "44",
        ("maintenance_completing", "maintenance_uncertain"): "45",
        ("maintenance_completing", "maintenance_finished"): "46",
        ("maintenance_uncertain", "deadman"): "49",
        ("maintenance_stopping", "maintenance_pass"): "55",
        ("maintenance_completing", "maintenance_pass"): "56",
    }
    if (phase, event) in maintenance:
        return _row(maintenance[phase, event])
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
        ("draining", "term"),
        ("draining", "drain_ended"),
        ("draining", "deadman"),
        *((phase, "teardown") for phase in PROCESS),
        *(("closed", event) for event in _EVENTS),
        *(
            (phase, verb)
            for phase in ("serving", "draining")
            for verb in ("SPAWN", "SIGNAL", "SHUTDOWN", "MAINTENANCE_STOP")
        ),
        *(
            (phase, verb)
            for phase in (
                "maintenance_uncertain",
                "maintenance_stopping",
                "maintenance_completing",
            )
            for verb in ("SPAWN", "SIGNAL", "SHUTDOWN", "MAINTENANCE_STOP")
        ),
        ("maintenance_stopping", "maintenance_pass"),
        ("maintenance_completing", "maintenance_pass"),
        ("maintenance_checking", "maintenance_refused"),
        ("maintenance_checking", "maintenance_stopping"),
        ("maintenance_checking", "maintenance_idle"),
        ("maintenance_stopping", "maintenance_idle"),
        ("maintenance_stopping", "maintenance_uncertain"),
        ("maintenance_completing", "maintenance_uncertain"),
        ("maintenance_completing", "maintenance_finished"),
        ("maintenance_uncertain", "term"),
        ("maintenance_completed", "term"),
        ("maintenance_uncertain", "deadman"),
    }
    # ten witnesses (serving by four leases); per event, the reached cells by
    # partition: start 3+3, publish 1+1, term 4+1+1+1, drained 2+2, pass_ended
    # 1+1, deadman 4+1, teardown 10, each verb four leases by four
    # credentials, and each verb in closed by four credentials; DL-401 adds each
    # verb in the two maintaining phases by four credentials (32) and maintenance_pass
    # in them and in closed (3); DL-416 adds draining by four leases: term, drain_ended,
    # deadman and teardown (16), each verb by four credentials (64), drain_ended in
    # closed (1)
    assert len(WITNESSES) == 19
    assert len(table) == 268


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
    teardown has no such cell: rows 31 to 35 take it in every state but
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
    own token is stale. In `closed` the final state ignores it."""
    executor, key = _witness(phase, "live")()
    request = runner_supervisor._Request({"cmd": verb, "incarnation": INCARNATION, "token": TOKEN})
    with pytest.raises(Exception) as caught:
        executor.dispatch(key, runner_supervisor.Event(verb, request))
    assert sc.is_statechart_error(caught.value)
    assert type(caught.value).__name__ == "UnhandledEvent"


@pytest.mark.parametrize("verb", ["SPAWN", "SIGNAL", "SHUTDOWN"])
def test_a_verb_in_closed_is_ignored_by_the_final_state(verb: str) -> None:
    executor, key = _witness("closed", "live")()
    request = runner_supervisor._Request({"cmd": verb, "incarnation": INCARNATION, "token": TOKEN})
    (outcome,) = executor.dispatch(key, runner_supervisor.Event(verb, request))
    assert (outcome.kind, outcome.rows, outcome.intents) == ("fired", (), ())
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
    sup._orderly_shutdown(runner_supervisor.TERM)  # 06, the drain
    assert sup.state == "draining"
    sup._orderly_shutdown(runner_supervisor.DRAIN_ENDED)  # 62, then the wait
    assert sup.state == "stopped"
    stand(sup, "shutting_down")
    sup._orderly_shutdown(runner_supervisor.PASS_ENDED)  # 12, then the wait
    sup._orderly_shutdown(runner_supervisor.TERM)  # 09
    assert sup.state == "stopped"
    # Maintenance procedure events have their own admission/force/teardown probes.
    maintenance = {
        "MAINTENANCE_STOP",
        "maintenance_refused",
        "maintenance_stopping",
        "maintenance_idle",
        "maintenance_uncertain",
        "maintenance_finished",
        "maintenance_pass",
    }
    assert set(posted) == set(alphabet(CHART)) - maintenance
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
    chart that lacks row 35, the teardown post finds no row: the statechart
    error replaces the refusal, and main prints the report under one prefix
    and exits 7, not 1 and not a traceback. Teardown stops at the post, so
    the process exit releases the rest. The run without the gap is the
    non-triggering case: exit 1. The gapped chart records its hits into a
    private directory, so its forbidden guard outcome stays out of the gate."""
    made: list[Any] = []

    class Recorded(runner_supervisor.Supervisor):
        def __init__(self, run_root: str, **kwargs: Any) -> None:
            super().__init__(run_root, **kwargs)
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
        rows = tuple(row for row in CHART.rows if row.id != _row("35"))
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


# ---- teardown by what the state owns; the retry of an errored wait


#: the ids out of use: .10 and .11 are the teardown rows .31 to .35 (DL-378), and .21 to .30
#: are the final `closed`'s implicit ignore (DL-380)
RETIRED = {_row("10"), _row("11"), *(_row(str(n)) for n in range(21, 31))}


def test_teardown_is_a_row_per_source_state_in_three_ownership_classes() -> None:
    """Five teardown rows, one per source state, in three classes: the
    published class (up, stopped) and the private class (bound) each report
    their unlink; the third class (starting, refused) reports nothing. The two retry
    cells are internal rows with the same action. A retired id stays out."""
    teardown = [
        (row.id, row.source, row.action)
        for row in CHART.rows
        if row.event == runner_supervisor.TEARDOWN and row.kind == "external"
    ]
    assert teardown == [
        (_row("31"), "up", "unlink_published"),
        (_row("32"), "stopped", "unlink_published"),
        (_row("33"), "bound", "unlink_private"),
        (_row("34"), "starting", ""),
        (_row("35"), "refused", ""),
        (_row("47"), "maintenance_completed", ""),
    ]
    retries = [
        (row.id, row.source, row.event, row.kind) for row in CHART.rows if row.action == "retry"
    ]
    assert retries == [
        (_row("12"), "shutting_down", runner_supervisor.PASS_ENDED, "internal"),
        (_row("36"), "shutting_down", runner_supervisor.TERM, "internal"),
    ]
    assert len(CHART.rows) == 57
    assert {row.id for row in CHART.rows} & RETIRED == set()  # a retired id stays out (DL-364)
    ctx = runner_supervisor.Context("process", runner_supervisor.Event("x"), frozenset(), None)
    intents = {name: action(ctx).intents for name, action in CHART.actions.items()}
    assert intents == {
        "refuse": (runner_supervisor.REFUSE,),
        "hold": (runner_supervisor.HOLD,),
        "drain": (runner_supervisor.DRAIN,),
        "retry": (runner_supervisor.FLUSH, runner_supervisor.DRAIN),
        "advance": (runner_supervisor.ADVANCE,),
        "complete": (runner_supervisor.COMPLETE,),
        "unlink_published": (runner_supervisor.UNLINK_PUBLISHED,),
        "unlink_private": (runner_supervisor.UNLINK_PRIVATE,),
    }


#: Every cell of the chart at ec4ecbd, before teardown's rows reported what they unlink,
#: recorded once by "witness|event|partition": the outcome count, kind, rows, ignored
#: rows, refusal codes, intents and configuration, or the error class it raised.
_AT_BASE: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parent / "supervisor_process_cells.json").read_text()
)
_PUBLISHED = [runner_supervisor.UNLINK_PUBLISHED]
_RETRY = [runner_supervisor.FLUSH, runner_supervisor.DRAIN]
#: the declared differences, cell by cell: the row the cell takes now and its intents
_DECLARED: dict[str, tuple[str, list[str]]] = {
    **{f"serving/{lease}|teardown|": (_row("31"), _PUBLISHED) for lease in LEASE_PHASES},
    "shutting_down|teardown|": (_row("31"), _PUBLISHED),
    "stopped|teardown|": (_row("32"), _PUBLISHED),
    "bound|teardown|": (_row("33"), [runner_supervisor.UNLINK_PRIVATE]),
    "starting|teardown|": (_row("34"), []),
    "refused|teardown|": (_row("35"), []),
    "shutting_down|term|": (_row("36"), _RETRY),
    "shutting_down|pass_ended|": (_row("12"), _RETRY),
    **{f"serving/{lease}|term|": (_row("06"), [runner_supervisor.HOLD]) for lease in LEASE_PHASES},
}
#: the declared cells that move elsewhere than at the base: a signal drains (DL-416)
_MOVED = {f"serving/{lease}|term|": sorted(PROCESS["draining"]) for lease in LEASE_PHASES}


def _cell(witness: str, event: str, payload: Any) -> dict[str, Any]:
    executor, key = WITNESSES[witness]()
    try:
        outcomes = executor.dispatch(key, runner_supervisor.Event(event, payload))
    except Exception as error:
        assert sc.is_statechart_error(error), error
        return {"raises": type(error).__name__}
    return {
        "outcomes": len(outcomes),
        "kind": outcomes[0].kind,
        "rows": list(outcomes[0].rows),
        "ignored": list(outcomes[0].ignored),
        "refusals": list(outcomes[0].refusals),
        "intents": [str(i) for o in outcomes for i in o.intents],
        "active": sorted(executor.active(key)),
    }


def test_every_cell_answers_as_before_but_the_declared_fifteen() -> None:
    """All 220 cells, ten witnesses by every event and partition: 124 raise
    as before, and every other cell keeps its outcome count, kind, ignored
    rows, refusal codes, intents and configuration. The fifteen declared
    cells keep their kind and take the new row with its intents: the teardown
    cells report what the state owns, and both retry cells report the flush
    before the drain. Each keeps its configuration, except a signal while
    serving, which starts the drain (DL-416). The 22 cells of `closed`, each
    recorded with a retired root row as its one ignored row, keep everything
    but the row: the final state ignores the event with no row (DL-380)."""
    partitions = _partitions()
    assert len(_AT_BASE) == 220
    assert sum(1 for cell in _AT_BASE.values() if "raises" in cell) == 124
    assert set(_DECLARED) <= set(_AT_BASE) and len(_DECLARED) == 15
    closed = [cell for cell in _AT_BASE if cell.startswith("closed|")]
    assert len(closed) == 22
    for cell, recorded in _AT_BASE.items():
        witness, event, label = cell.split("|")
        expected = dict(recorded)
        if cell in _DECLARED:
            row, expected["intents"] = _DECLARED[cell]
            expected["rows"] = [row]
        if cell in _MOVED:
            expected["active"] = _MOVED[cell]
        if cell in closed:
            assert recorded["rows"] == recorded["ignored"] and set(recorded["rows"]) <= RETIRED
            expected["rows"] = expected["ignored"] = []
        payload = partitions[event][label] if label else None
        assert _cell(witness, event, payload) == expected, cell


@pytest.mark.parametrize("phase", ["serving", "draining", "shutting_down", "stopped"])
def test_teardown_from_a_published_state_unlinks_the_published_socket_and_pid_only(
    short_root: Path, phase: str
) -> None:
    """supervisor_process.31 and .32. supervisor.sock and supervisor.pid are
    this incarnation's and go; a file at the private path stays, since the
    published class does not unlink it."""
    sup = runner_supervisor.Supervisor(str(short_root))
    sup._bind()
    if phase != "serving":
        sup._process(runner_supervisor.TERM)  # .06
    if phase in ("shutting_down", "stopped"):
        sup._process(runner_supervisor.TERM)  # .61; the wait is not run here
    if phase == "stopped":
        sup._process(runner_supervisor.DRAINED, runner_supervisor.REAPED)
    assert sup.state == phase
    decoy = Path(sup._private_path)
    decoy.write_text("a file at the private path")
    sup._teardown()
    assert sup.state == "closed"
    assert not Path(sup.sock_path).exists() and not Path(sup.pid_path).exists()
    assert decoy.read_text() == "a file at the private path"


def test_teardown_from_bound_unlinks_the_private_socket_and_pid_only(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """supervisor_process.33. The rename fails after the pid record is
    written: the private socket and supervisor.pid are this incarnation's
    and go. A hard link to the private socket at the published path passes
    the published unlink's inode check, so only the class keeps it: a .33
    that reported the published unlink would remove it."""
    sup = runner_supervisor.Supervisor(str(short_root))
    rename = runner_supervisor.os.rename

    def fail(source: str, destination: str) -> None:
        if destination == sup.sock_path:
            runner_supervisor.os.link(source, destination)
            raise OSError("synthetic publication failure")
        rename(source, destination)

    monkeypatch.setattr(runner_supervisor.os, "rename", fail)
    with pytest.raises(OSError, match="synthetic"):
        sup._bind()
    assert sup.state == "bound" and Path(sup._private_path).exists()
    assert json.loads(Path(sup.pid_path).read_text())["incarnation"] == sup.incarnation
    sup._teardown()
    assert sup.state == "closed"
    assert not Path(sup._private_path).exists() and not Path(sup.pid_path).exists()
    assert Path(sup.sock_path).stat().st_ino == sup._socket_inode  # the link stays
    Path(sup.sock_path).unlink()


@pytest.mark.parametrize("phase", ["starting", "refused"])
def test_teardown_from_starting_or_refused_unlinks_nothing(short_root: Path, phase: str) -> None:
    """supervisor_process.34 and .35. Nothing is this incarnation's: a pid
    record that names it, a file at the private path and one at the
    published path all stay."""
    sup = runner_supervisor.Supervisor(str(short_root))
    held = None
    if phase == "refused":
        held = runner_supervisor.flock_exclusive(short_root / "supervisor.lock")
        with pytest.raises(SystemExit) as refusal:
            sup._bind()  # .01
        assert isinstance(refusal.value.__cause__, runner_supervisor.LockHeld)
    try:
        assert sup.state == phase
        runner_supervisor.durable_write_json(sup.pid_path, {"incarnation": sup.incarnation})
        for path in (sup._private_path, sup.sock_path):
            Path(path).write_text("a decoy")
        sup._teardown()
    finally:
        if held is not None:
            runner_supervisor.os.close(held)
    assert sup.state == "closed"
    assert json.loads(Path(sup.pid_path).read_text()) == {"incarnation": sup.incarnation}
    assert Path(sup._private_path).exists() and Path(sup.sock_path).exists()
    Path(sup._private_path).unlink()


# ---- the signal latch


def test_a_refused_shutdown_keeps_a_latched_signal(
    queued_supervisor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGTERM latched before a SHUTDOWN with a stale token: the refusal
    leaves the latch set, so the loop's TERM still drains from serving
    (.06), and the drain's end runs the wait (.62) that ends it (.07)."""
    fence = _Fence(queued_supervisor, monkeypatch, "SHUTDOWN")
    fence.sup._on_term_signal(runner_supervisor.signal.SIGTERM, None)
    assert fence.ask(incarnation=fence.sup.incarnation, token=fence.token + 1) == _STALE
    assert fence.sup._signals and fence.sup.state == "serving"
    fence.sup._orderly_shutdown(runner_supervisor.TERM)
    assert fence.sup.state == "draining"
    fence.sup._orderly_shutdown(runner_supervisor.DRAIN_ENDED)
    assert fence.rows == [fence.stale_row, _row("06"), _row("62"), _row("07")]
    assert fence.sup.state == "stopped" and not fence.sup._signals


def test_a_signal_during_a_successful_shutdown_wait_is_a_no_op_at_stopped(
    queued_supervisor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGTERM latched while a SHUTDOWN's wait runs: the wait ends the
    process (.07), and the loop's TERM finds it stopped, where .09 reports
    nothing and no second wait runs. A latch set before the SHUTDOWN is the
    non-triggering case: the SHUTDOWN's handler clears it."""
    fence = _Fence(queued_supervisor, monkeypatch, "SHUTDOWN")
    fence.sup._on_term_signal(runner_supervisor.signal.SIGTERM, None)
    waits: list[str] = []
    wait = fence.sup._shutdown_wait

    def latching_wait() -> None:
        assert not fence.sup._signals  # cleared before the wait
        waits.append(fence.sup.state)
        fence.sup._on_term_signal(runner_supervisor.signal.SIGTERM, None)
        wait()

    monkeypatch.setattr(fence.sup, "_shutdown_wait", latching_wait)
    assert fence.current() == {"ok": True}
    assert fence.sup.state == "stopped" and fence.sup._signals
    fence.sup._orderly_shutdown(runner_supervisor.TERM)
    assert fence.rows == [fence.passing_row, _row("07"), _row("09")]
    assert waits == ["shutting_down"] and not fence.sup._signals


def test_a_signal_between_the_retry_s_flush_and_its_wait_stays_latched_for_stopped(
    queued_supervisor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGTERM latched while .12 sends the queued replies: the latch was
    cleared before the post, so the signal stays latched and does not stop
    the wait. The wait runs once and ends the process (.07). In `run()` the
    loop then exits and teardown runs from stopped, so the loop posts no
    TERM; the test posts the latched TERM by hand, and .09 runs nothing."""
    sup, connect = queued_supervisor
    connect()
    monkeypatch.setattr(sup, "_reap", lambda: None)
    sup._process(runner_supervisor.TERM)  # .06: the drain
    sup._process(runner_supervisor.TERM)  # .61: the wait that errs is not run here
    assert sup.state == "shutting_down"
    rows: list[str] = []
    dispatch = sup._machines.dispatch

    def spy(key: Any, event: Any) -> Any:
        outcomes = dispatch(key, event)
        if key == "process":
            rows.extend(row for o in outcomes for row in o.rows)
        return outcomes

    sup._machines.dispatch = spy
    writable = sup._writable

    def latching(conn: Any, *, resume: bool = True) -> None:
        sup._on_term_signal(runner_supervisor.signal.SIGTERM, None)
        writable(conn, resume=resume)

    waits: list[str] = []
    wait = sup._shutdown_wait

    def counted() -> None:
        waits.append(sup.state)
        wait()

    monkeypatch.setattr(sup, "_writable", latching)
    monkeypatch.setattr(sup, "_shutdown_wait", counted)
    sup._orderly_shutdown(runner_supervisor.PASS_ENDED)
    assert sup._signals and sup.state == "stopped"
    sup._orderly_shutdown(runner_supervisor.TERM)
    assert rows == [_row("12"), _row("07"), _row("09")]
    assert waits == ["shutting_down"] and not sup._signals
