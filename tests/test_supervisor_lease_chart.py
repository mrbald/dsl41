"""supervisor_lease as a statechart (docs/statechart-engine.md §16).

The chart is well formed and draws no warning; the generated completeness test
runs every witness configuration by every event and payload partition, and a
table written from supervisor-protocol §5, not from the rows, pins each cell;
and every event name the supervisor posts is in the chart's alphabet.
"""

from __future__ import annotations

import json
import os
import selectors
import socket
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
import statechart_completeness as sc

from dsl41 import runner_supervisor
from dsl41.state_machine import alphabet, chart_warnings, well_formed

CHART = runner_supervisor.SUPERVISOR_LEASE
INCARNATION = "a" * 32
TOKEN = 7
NOW = 1000.0
WALL = 1_700_000_000.0
#: the holder's connection in a witness; any other object is another connection
HOLDER = object()
OTHER = object()


def test_the_chart_is_well_formed_and_draws_no_warning() -> None:
    assert well_formed(CHART) == []
    assert chart_warnings(CHART) == []
    assert CHART.clock == "monotonic"


# ---- the abstraction (§13)


def _witness(phase: str) -> sc.Builder:
    """A lease in `phase`, on a stand-in for the supervisor that holds what the
    guards and actions read and write: the record, the incarnation and the
    token counter. The token is 7; an expired deadline is the instant itself.
    The executor is the supervisor's own, from its by-path copy of the module."""
    parent = "held" if phase in ("live", "orphaned") else "unheld"

    def build() -> tuple[Any, str]:
        lease = None
        if phase != "free":
            deadline = NOW if phase == "expired" else NOW + 60
            conn = None if phase == "orphaned" else HOLDER
            lease = runner_supervisor._Lease("h", TOKEN, deadline, "then", conn)
        executor = runner_supervisor.Executor()
        data = SimpleNamespace(
            lease=lease, incarnation=INCARNATION, _next_token=TOKEN + 1, _machines=executor
        )
        executor.restore("lease", CHART, {"lease", parent, phase}, data)
        return executor, "lease"

    return build


_CREDENTIALS: dict[str, dict[str, Any]] = {
    "current": {"incarnation": INCARNATION, "token": TOKEN},
    "stale": {"incarnation": INCARNATION, "token": TOKEN + 1},
    "foreign": {"incarnation": "b" * 32, "token": TOKEN},
    "none": {},
}
#: ttl_s: a number, not a number, and a number with no representable expiry
_TTLS: dict[str, Any] = {"ok": 60, "unparsed": "x", "unrepresentable": float("inf")}


def _request(req: dict[str, Any]) -> runner_supervisor._LeaseRequest:
    return runner_supervisor._LeaseRequest(req, OTHER, NOW, WALL)


def _partitions() -> dict[str, dict[str, Any]]:
    # an unnamed ACQUIRE with each ttl_s: controller_id is checked first (DL-150)
    acquire = {
        f"unnamed/{ttl}": _request({"controller_id": "", "ttl_s": value})
        for ttl, value in _TTLS.items()
    }
    renew = {}
    for creds, fields in _CREDENTIALS.items():
        for ttl, value in _TTLS.items():
            label = f"{creds}/{ttl}"
            acquire[label] = _request({"controller_id": "c", "ttl_s": value, **fields})
            renew[label] = _request({"ttl_s": value, **fields})
    return {
        "ACQUIRE": acquire,
        "RENEW": renew,
        "RELEASE": {creds: _request(dict(fields)) for creds, fields in _CREDENTIALS.items()},
        runner_supervisor.DISCONNECT: {"holder": HOLDER, "other": OTHER},
    }


PHASES = ("free", "expired", "live", "orphaned")
ABSTRACTION = sc.Abstraction(
    witnesses={phase: _witness(phase) for phase in PHASES}, partitions=_partitions()
)


def _row(number: str) -> str:
    return f"supervisor_lease.{number}"


def _section_5(phase: str, event: str, label: str) -> str:
    """The cell as supervisor-protocol §5 states it, written without the rows.

    A lease is unexpired when live or orphaned. ACQUIRE checks controller_id
    first, then that ttl_s is a number; a live lease yields only to this
    incarnation and the current token, and any other lease is granted. RENEW
    and RELEASE check the incarnation, then the token of an unexpired lease;
    RENEW then needs a representable expiry. A ttl_s with no representable
    expiry is answered `internal:` wherever it would be computed (DL-300). Only
    the holder's connection closing changes the record. An expiry moves an
    unexpired lease to expired."""
    unexpired = phase in ("live", "orphaned")
    if event == runner_supervisor.EXPIRE:
        return _row("17") if unexpired else f"ignored {_row('18')}"
    if event == runner_supervisor.DISCONNECT:
        closes = {"live": "14", "expired": "15"}
        if label == "holder" and phase in closes:
            return _row(closes[phase])
        return f"ignored {_row('16')}"
    creds, _, ttl = label.partition("/")
    if event == "ACQUIRE":
        if creds == "unnamed":
            return "refused bad_controller_id"
        if ttl == "unparsed":
            return "refused internal"
        if phase == "live" and creds != "current":
            return "refused lease_held"
        if ttl == "unrepresentable":
            return "refused internal"
        return {"live": _row("03"), "orphaned": _row("02")}.get(phase, _row("01"))
    if creds in ("foreign", "none"):
        return "refused wrong_incarnation"
    if creds == "stale" or not unexpired:
        return "refused stale_token"
    if event == "RELEASE":
        return _row("11")
    return _row("07") if ttl == "ok" else "refused internal"


def test_the_completeness_table_is_section_5() -> None:
    table = sc.completeness_table(CHART, ABSTRACTION)
    expected = {
        (phase, event, label): _section_5(phase, event, label) for phase, event, label in table
    }
    assert table == expected
    events = {event for _, event, _ in table}
    assert events == set(alphabet(CHART))
    # every witness by every event and partition; expire has no generation
    per_witness = 15 + 12 + 4 + 2 + 1
    assert len(table) == len(PHASES) * per_witness


# ---- the alphabet (DL-325)


def test_every_event_the_supervisor_posts_is_in_the_alphabet(queued_supervisor: Any) -> None:
    """The posters: the dispatch belt for every verb it admits, the dropped
    connection, and the sampled expiry. No action raises an event: every
    dispatch is one microstep at depth 0. The executor also runs the process
    chart, whose posts (the mutating verbs) are not the lease's."""
    sup, connect = queued_supervisor
    posted: list[str] = []
    dispatch: Callable[..., Any] = sup._machines.dispatch

    def spy(key: Any, event: Any) -> Any:
        outcomes = dispatch(key, event)
        assert [o.depth for o in outcomes] == [0], outcomes
        if key == "lease":
            posted.append(event.name)
        return outcomes

    sup._machines.dispatch = spy
    conn, peer = connect()
    stale = {"incarnation": sup.incarnation, "token": 99}
    for verb in ("PING", "LIST", "ACQUIRE", "RENEW", "RELEASE", "SPAWN", "SIGNAL", "SHUTDOWN"):
        request = {"v": 1, "cmd": verb, "controller_id": "c", "spec": {}, **stale}
        sup._dispatch(conn, json.dumps(request).encode("utf-8"))
    assert sup.lease is not None
    sup.lease.deadline = 0
    assert sup._lease_phase() == "expired"
    sup._drop_conn(conn)
    peer.close()
    assert set(posted) == set(alphabet(CHART))
    assert sup.state == "serving"  # the refused SHUTDOWN stopped nothing


# ---- the sampled expiry and the report


def test_an_expired_lease_posts_one_expiry_and_keeps_the_report_history(
    queued_supervisor: Any, monkeypatch: Any
) -> None:
    """The supervisor posts `expire` once per deadline, while the lease is held.
    Sampling an expired lease again posts nothing, so the executor's recent
    microsteps, which a statechart error reports, keep what led up to it. A
    renewal sets a new deadline, and its expiry is posted once more."""
    clock = SimpleNamespace(now=NOW, monotonic=lambda: clock.now, time=lambda: WALL)
    monkeypatch.setattr(runner_supervisor, "time", clock)
    sup, connect = queued_supervisor
    conn, peer = connect()
    sup._dispatch(conn, b'{"v":1,"cmd":"ACQUIRE","controller_id":"h","ttl_s":10}')
    clock.now = NOW + 10
    for _ in range(100):
        assert sup._lease_phase() == "expired"
    expiries = [line for line in sup._machines._recent if " expire " in line]
    assert len(expiries) == 1 and "supervisor_lease.17" in expiries[0]
    leases = [line for line in sup._machines._recent if line.startswith("'lease' ")]
    assert len(leases) == 3  # the start, the ACQUIRE, the expiry
    sup._drop_conn(conn)
    peer.close()


def test_a_lease_request_reports_without_its_token() -> None:
    request = _request({"cmd": "RENEW", "incarnation": INCARNATION, "token": 4242, "ttl_s": 5})
    assert repr(request) == (
        f"_LeaseRequest({{'cmd': 'RENEW', 'incarnation': '{INCARNATION}', 'ttl_s': 5}})"
    )
    assert "4242" not in repr(request)


def test_teardown_leaves_the_lease_alone(short_root: Any) -> None:
    """Teardown drops every connection after the loop ended, perhaps on an
    error. It posts nothing to the lease, so a fault there cannot replace the
    error that ended the loop (an OSError keeps exit 1) or cut teardown short.
    Its own `teardown` post goes to the process chart."""
    sup = runner_supervisor.Supervisor(str(short_root))
    sup._process(runner_supervisor.START, runner_supervisor.FREE)
    sup._process(runner_supervisor.PUBLISH)
    server, peer = socket.socketpair()
    conn = runner_supervisor._Conn(server)
    sup._conns[server.fileno()] = conn
    sup._sel.register(server, selectors.EVENT_READ, ("conn", conn))

    dispatch: Callable[..., Any] = sup._machines.dispatch

    def refuse(key: Any, event: Any) -> Any:
        if key == "lease":
            raise runner_supervisor.StatechartError("no lease post during teardown")
        return dispatch(key, event)

    sup._machines.dispatch = refuse
    sup._teardown()
    peer.close()
    assert sup.state == "closed" and sup._conns == {} and server.fileno() == -1
    for fd in (sup._chld_r, sup._chld_w):
        with pytest.raises(OSError):
            os.fstat(fd)
