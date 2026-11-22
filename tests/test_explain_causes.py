"""`explain` says why a job has not started (DL-534): what wakes it, what the oracle would do
with one named attempt now and which gate decides, what the pool is short of and who holds it,
and what keeps a decided start from its dispatch.

The disposition is checked against the oracle itself: each answer's attempt is then made on a
fork of the oracle (`_outcome`), a condition wake's `start` or a full scan's `readmit`, and what
the fork did must be what the answer said. Every answer is also checked to leave the oracle's
state digest, trace, instant and emitted log as they were (`_explain`).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from estate_gen import SealPoint
from oracle_goldens import sample_runs
from oracle_inputs import Side, own, seal_digest
from test_engine_stops_branches import RUN_ID, _adopt
from test_engine_stops_cookie import PGID
from test_update_atomic import _Ladders
from test_update_member_holds import _estate, _start
from test_update_owned_processes import _observed, _started, _teardown
from test_update_settlement import _close, _plan, _request

from dsl41.ir import lower_source
from dsl41.job_chart import START_GATES, readmit_verdict
from dsl41.job_chart_events import READMIT, START
from dsl41.job_chart_rows import row_configuration
from dsl41.oracle import Oracle
from dsl41.oracle_explain import reading, start_answer, wake
from dsl41.oracle_facts import ReadmitFacts, job_data
from dsl41.oracle_state import Event
from dsl41.reload_update import Admitted
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_control import ControlServer
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import resume_run, start_run
from dsl41.semantics import resolve
from dsl41.state_machine import Context
from dsl41.state_machine import Event as ChartEvent

#: The unarmed date gate's text (SEM-32).
UNARMED = (
    "dated and not armed: a condition wake or a hold release stops here; a STARTJOB, a"
    " scheduled tick's or an operator's, passes"
)

#: A Wednesday morning.
T0 = datetime(2026, 7, 1, 8, 0)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _ev(kind: str, job: str, minutes: float = 0, **payload: object) -> Event:
    return Event(at=_at(minutes), kind=kind, payload={"job": job, **payload})


def _set(name: str, value: str, minutes: float = 0) -> Event:
    return Event(at=_at(minutes), kind="SET_GLOBAL", payload={"name": name, "value": value})


def _status(job: str, status: str, minutes: float = 0) -> Event:
    return _ev("STATUS", job, minutes, status=status)


def _oracle(text: str, *events: Event, switches: dict[str, str] | None = None) -> Oracle:
    """An oracle over `text` with `events` fed; with none, one time observation at T0."""
    oracle = Oracle(lower_source(text), semantics=resolve(switches or {}))
    if not events:
        oracle.advance(T0)
    for event in events:
        oracle.feed(event)
    return oracle


def _state(oracle: Oracle) -> tuple[Any, ...]:
    """What a read must leave as it was: the store's seal digest, the rows' names, the trace's
    and the emitted log's lengths and the oracle's instant."""
    digest, _ = seal_digest(oracle, oracle._now or T0)
    return digest, tuple(oracle.store.job), len(oracle._trace), len(oracle._emitted), oracle._now


def _explain(oracle: Oracle, job: str) -> dict[str, Any]:
    before = _state(oracle)
    answer = start_answer(oracle, job)
    assert _state(oracle) == before
    return answer


def _outcome(oracle: Oracle, job: str, answer: dict[str, Any]) -> str:
    """What the oracle does with the answer's attempt, made on a fork at its instant: a
    condition wake's start, or a full queue scan for a waiter. A deferral that finds its timer
    already pending writes nothing, so the pending timer at the answer's opening is its mark."""
    attempt = answer["attempt"]
    fork = oracle.fork()
    with fork.batch(fork._now):
        if attempt == "readmit":
            fork._root_scan(job, keep_stopped=False)
        else:
            fork._attempt_root(job, START, "probe")
    lines = [(entry.transition, entry.cause) for entry in fork._trace if entry.job == job]
    if not lines:
        if answer["disposition"] == "defer":
            [opening] = [gate["at"] for gate in answer["gates"] if gate["kind"] == "window"]
            pending = {(due.isoformat(), name, kind) for due, name, kind in fork.pending_timers()}
            if (opening, job, "run_window") in pending:
                return "defer"
        return "keep" if attempt == "readmit" else "ignored"
    transition, cause = lines[0]
    target = transition.partition("->")[2]
    if transition == "RUN_WINDOW_DEFER":
        return "defer"
    if transition == "RUN_WINDOW_SKIP" or (target == "INACTIVE" and "run_window skip" in cause):
        return "skip"
    if target in ("STARTING", "RUNNING"):
        return "admit"
    if target == "QUE_WAIT":
        return "queue"
    if target == "SUCCESS" and "ON_NOEXEC bypass" in cause:
        return "bypass"
    if attempt == "readmit" and target == "INACTIVE":
        return "leave" if "QUE_WAIT left unstarted" in cause else "cancel"
    return f"unclassified {lines[0]}"


def _readmitted(oracle: Oracle, job: str) -> str:
    """A waiter's isolated readmission on a fork, unseeded: `readmit_verdict` over the chart's
    own payload, a full scan's (`keep_stopped` false), as the disposition names it."""
    fork = oracle.fork()
    row = fork.store.job[job]
    facts = ReadmitFacts(fork, job, fork.catalog.jobs[job], keep_stopped=False)
    context = Context(
        key=fork.store.job_key(job),
        event=ChartEvent(READMIT, facts),
        active=row_configuration(row),
        data=job_data(fork, job),
    )
    verdict = readmit_verdict(context)
    if verdict == "keep":
        return "held" if row.on_hold else "short"
    return {"admit": "fits", "cancel": "cancel", "leave": "leave"}[verdict]


#: What a full scan does with a waiter that nothing ahead of it changes.
_SCANNED = {"fits": "admit", "short": "keep", "held": "keep", "cancel": "cancel", "leave": "leave"}


def _agrees(oracle: Oracle, job: str, answer: dict[str, Any]) -> bool:
    """Whether the oracle does what the answer says. A start's disposition is what a fork does
    with the attempt. A waiter's is its isolated readmission; with no waiter ahead of it that
    needs its buckets, it is also what a full scan does with it."""
    disposition = answer["disposition"]
    if answer["attempt"] != "readmit":
        return disposition == _outcome(oracle, job, answer)
    if _readmitted(oracle, job) != disposition:
        return False
    return bool(answer["capacity"]["ahead"]) or _SCANNED[disposition] == _outcome(
        oracle, job, answer
    )


def _decides(oracle: Oracle, job: str) -> dict[str, Any]:
    """The answer for `job`, checked against what the oracle then does with its attempt."""
    answer = _explain(oracle, job)
    assert _agrees(oracle, job, answer), answer
    return answer


def _gates(answer: dict[str, Any]) -> list[tuple[str, str]]:
    return [(gate["kind"], gate["state"]) for gate in answer["gates"]]


def _deciding(answer: dict[str, Any]) -> list[str]:
    return [gate["kind"] for gate in answer["gates"] if gate["state"] == "deciding"]


def _job(name: str, body: str = "") -> str:
    return f"insert_job: {name}\njob_type: c\ncommand: x\n{body}\n"


def _box(name: str, body: str = "") -> str:
    return f"insert_job: {name}\njob_type: b\n{body}\n"


#: A dated job whose tick is at 08:00 and whose window is 10:00 to 11:00.
_DATED = 'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\nrun_window: "10:00-11:00"\n'

#: One unit of the renewable R.
_R = "insert_resource: R\nres_type: R\namount: 1\n\n"
_NEEDS_R = "resources: (R, QUANTITY=1, FREE=A)\n"


# ---------------------------------------------------------------- one case per disposition


def test_dl534_a_plain_job_is_admitted_and_every_applicable_gate_is_open() -> None:
    answer = _decides(_oracle(_job("a")), "a")
    assert answer["as_of"] == T0.isoformat()
    assert (answer["attempt"], answer["disposition"]) == ("start", "admit")
    assert _gates(answer) == [("ice", "open"), ("hold", "open"), ("noexec", "open")]
    assert "capacity" not in answer


def test_dl534_a_job_short_of_capacity_queues_and_names_the_holder() -> None:
    oracle = _oracle(_R + _job("h", _NEEDS_R) + _job("q", _NEEDS_R), _ev("STARTJOB", "h"))
    answer = _decides(oracle, "q")
    assert answer["disposition"] == "queue"
    assert _deciding(answer) == ["capacity"]
    assert answer["capacity"] == {
        "admissible": False,
        "blockers": [],
        "short": [
            {
                "bucket": "r:R",
                "capacity": 1,
                "demand": 1,
                "used": 1,
                "short": 1,
                "spent": 0,
                "holders": [{"job": "h", "units": 1}],
            }
        ],
    }


def test_dl534_a_noexec_job_bypasses() -> None:
    answer = _decides(_oracle(_job("n"), _ev("ON_NOEXEC", "n")), "n")
    assert answer["disposition"] == "bypass"
    assert answer["gates"][-1] == {
        "kind": "noexec",
        "state": "deciding",
        "text": "on noexec: completes without running",
        "job": None,
        "at": None,
    }


def test_dl534_a_member_under_a_noexec_box_names_the_box() -> None:
    """SEM-22: a member taken off noexec under a box on noexec still bypasses, and the gate
    names the box. Held in the box's run, the hold decides and the bypass is context."""
    text = _box("B") + _job("m", "box_name: B\n")
    events = (_ev("ON_NOEXEC", "B"), _ev("OFF_NOEXEC", "m"), _ev("ON_HOLD", "m"))
    oracle = _oracle(text, *events, _ev("STARTJOB", "B"))
    assert oracle.store.job["B"].status == "RUNNING"
    answer = _decides(oracle, "m")
    assert answer["disposition"] == "ignored"
    [gate] = [gate for gate in answer["gates"] if gate["kind"] == "noexec"]
    assert gate == {
        "kind": "noexec",
        "state": "context",
        "text": "box 'B' is on noexec: completes without running",
        "job": "B",
        "at": None,
    }
    oracle.feed(_ev("OFF_HOLD", "m", 1))
    assert oracle.store.job["m"].status == "SUCCESS"


def _armed(condition_false_until: float, *later: Event) -> Oracle:
    """`d`, dated, armed by its 08:00 tick while its condition was false; `G` is set at
    `condition_false_until` minutes."""
    text = _job("d", _DATED + 'condition: v(G) = "go"\n')
    events = [_ev("STARTJOB", "d"), _set("G", "go", condition_false_until), *later]
    return _oracle(text, *events)


def test_dl534_outside_the_window_nearer_the_opening_defers_with_the_opening() -> None:
    oracle = _armed(30)
    assert oracle.store.job["d"].armed and oracle.store.job["d"].status == "INACTIVE"
    answer = _decides(oracle, "d")
    assert answer["disposition"] == "defer"
    [window] = [gate for gate in answer["gates"] if gate["kind"] == "window"]
    assert window["state"] == "deciding"
    assert window["at"] == datetime(2026, 7, 1, 10, 0).isoformat()
    assert ("date", "open") in _gates(answer)


def test_dl534_outside_the_window_nearer_the_close_skips() -> None:
    """At 12:00 the 11:00 close is nearer than tomorrow's opening: the start is not run. An
    INACTIVE job writes its skip line and does not move; a completed one moves to INACTIVE."""
    oracle = _armed(240)
    answer = _decides(oracle, "d")
    assert (answer["disposition"], _deciding(answer)) == ("skip", ["window"])
    oracle.feed(_status("d", "SUCCESS", 241))
    assert oracle.store.job["d"].armed
    answer = _decides(oracle, "d")
    assert (answer["disposition"], _deciding(answer)) == ("skip", ["window"])


def test_dl534_an_armed_job_whose_update_dropped_its_condition_is_admitted_inside_its_window() -> (
    None
):
    """spec-reload: a kept row keeps its arm and its new definition is read from the next input
    on; with its condition gone, no wake comes, and inside its window every gate is open."""
    # the 08:00 tick arms `d` on its false condition
    oracle = _oracle(_job("d", _DATED + 'condition: v(G) = "go"\n'), _ev("STARTJOB", "d"))
    oracle.replace_catalog(
        lower_source(_job("d", _DATED)), semantics=resolve({}), tz_aliases=None, default_tz=None
    )
    oracle.advance(_at(150))  # 10:30, inside the window
    assert oracle.store.job["d"].armed and oracle.store.job["d"].status == "INACTIVE"
    answer = _decides(oracle, "d")
    assert answer["disposition"] == "admit"
    assert _gates(answer) == [
        ("ice", "open"),
        ("hold", "open"),
        ("date", "open"),
        ("window", "open"),
        ("noexec", "open"),
    ]
    assert [gate["text"] for gate in answer["gates"]][2:4] == ["armed", "inside run_window"]
    tomorrow = datetime(2026, 7, 2, 8, 0)
    assert [entry["kind"] for entry in wake(oracle, "d", tomorrow)] == ["tick"]


@pytest.mark.parametrize(
    ("events", "deciding"),
    [
        ((_ev("ON_ICE", "c"),), "ice"),
        ((_ev("ON_HOLD", "c"),), "hold"),
        ((), "condition"),
    ],
)
def test_dl534_a_flag_or_a_false_condition_ignores_the_attempt(
    events: tuple[Event, ...], deciding: str
) -> None:
    answer = _decides(_oracle(_job("c", 'condition: v(G) = "go"\n'), *events), "c")
    assert (answer["disposition"], _deciding(answer)) == ("ignored", [deciding])


def test_dl534_a_dated_job_not_armed_ignores_a_condition_wake() -> None:
    """SEM-32: a dated job that no tick armed ignores a condition wake; a STARTJOB, here its
    08:00 tick's, passes the gate. The disposition is the condition wake's, so `arm` is no
    answer here."""
    oracle = _oracle(_job("d", _DATED))
    answer = _decides(oracle, "d")
    assert (answer["disposition"], _deciding(answer)) == ("ignored", ["date"])
    assert answer["gates"][2]["text"] == UNARMED
    oracle.feed(_ev("STARTJOB", "d"))  # the tick, outside the window: it defers
    assert [kind for _due, _job, kind in oracle.pending_timers()] == ["run_window"]


def test_dl534_a_member_of_a_box_that_is_not_running_is_ignored_and_names_the_box() -> None:
    oracle = _oracle(_box("B") + _job("m", "box_name: B\n"))
    answer = _decides(oracle, "m")
    assert answer["disposition"] == "ignored"
    [gate] = [gate for gate in answer["gates"] if gate["state"] == "deciding"]
    assert gate == {
        "kind": "box",
        "state": "deciding",
        "text": "box 'B' is not running",
        "job": "B",
        "at": None,
    }


def test_dl534_a_member_that_ran_in_this_box_run_is_ignored() -> None:
    text = _box("B") + _job("m", "box_name: B\n") + _job("n", "box_name: B\n")
    oracle = _oracle(text, _ev("STARTJOB", "B"), _status("m", "SUCCESS"))
    assert oracle.store.job["B"].status == "RUNNING"
    answer = _decides(oracle, "m")
    [gate] = [gate for gate in answer["gates"] if gate["state"] == "deciding"]
    assert (answer["disposition"], gate["text"]) == (
        "ignored",
        "already ran in this run of box 'B'",
    )


def test_dl534_a_member_taken_off_ice_in_its_box_run_is_ignored() -> None:
    """SEM-20 under the default `off-ice-in-running-box=next-run`."""
    text = _box("B") + _job("m", "box_name: B\n") + _job("n", "box_name: B\n")
    oracle = _oracle(text, _ev("ON_ICE", "m"), _ev("STARTJOB", "B"), _ev("OFF_ICE", "m"))
    answer = _decides(oracle, "m")
    [gate] = [gate for gate in answer["gates"] if gate["state"] == "deciding"]
    assert (answer["disposition"], gate["text"]) == (
        "ignored",
        "taken off ice in this run of box 'B': sits the run out",
    )


def test_dl534_a_running_job_is_running_and_its_gates_are_not_read() -> None:
    answer = _explain(_oracle(_job("a"), _ev("STARTJOB", "a")), "a")
    assert answer == {
        "as_of": T0.isoformat(),
        "attempt": None,
        "disposition": "running",
        "gates": [],
    }


# ---------------------------------------------------------------- two gates at once


@pytest.mark.parametrize(
    ("first", "second", "text", "events"),
    [
        ("ice", "hold", _job("j"), (_ev("ON_ICE", "j"), _ev("ON_HOLD", "j"))),
        ("hold", "date", _job("j", _DATED), (_ev("ON_HOLD", "j"),)),
        ("date", "box", _box("B") + _job("j", _DATED + "box_name: B\n"), ()),
        ("box", "condition", _box("B") + _job("j", 'box_name: B\ncondition: v(G) = "go"\n'), ()),
    ],
)
def test_dl534_of_two_failing_gates_the_chart_s_first_decides(
    first: str, second: str, text: str, events: tuple[Event, ...]
) -> None:
    answer = _decides(_oracle(text, *events), "j")
    states = dict(_gates(answer))
    assert (states[first], states[second]) == ("deciding", "context")
    assert START_GATES.index(first) + 1 == START_GATES.index(second)


def test_dl534_a_false_condition_decides_before_the_window() -> None:
    text = _job("d", _DATED + 'condition: v(G) = "go"\n')
    oracle = _oracle(text, _ev("STARTJOB", "d"), _set("G", "no", 30))
    answer = _decides(oracle, "d")
    states = dict(_gates(answer))
    assert (states["condition"], states["window"]) == ("deciding", "context")


def test_dl534_the_window_decides_before_noexec() -> None:
    oracle = _armed(30, _ev("ON_NOEXEC", "d", 31))
    answer = _decides(oracle, "d")
    assert answer["disposition"] == "defer"
    states = dict(_gates(answer))
    assert (states["window"], states["noexec"]) == ("deciding", "context")


def test_dl534_noexec_with_capacity_short_bypasses_and_capacity_does_not_decide() -> None:
    text = _R + _job("h", _NEEDS_R) + _job("n", _NEEDS_R)
    oracle = _oracle(text, _ev("STARTJOB", "h"), _ev("ON_NOEXEC", "n"))
    answer = _decides(oracle, "n")
    assert answer["disposition"] == "bypass"
    states = dict(_gates(answer))
    assert (states["noexec"], states["capacity"]) == ("deciding", "context")
    assert answer["capacity"]["admissible"] is False


# ---------------------------------------------------------------- a waiter


def test_dl534_a_waiter_is_answered_by_its_readmission() -> None:
    oracle = _oracle(_R + _job("h", _NEEDS_R) + _job("q", _NEEDS_R), _ev("STARTJOB", "h"))
    oracle.feed(_ev("STARTJOB", "q"))
    assert oracle.store.job["q"].status == "QUE_WAIT"
    answer = _decides(oracle, "q")
    assert (answer["attempt"], answer["disposition"]) == ("readmit", "short")
    assert _gates(answer) == [("hold", "open"), ("capacity", "deciding")]
    assert wake(oracle, "q", None) == [
        {
            "kind": "scan",
            "text": "a queue scan: units are released or a block lifts",
            "job": None,
            "at": None,
        }
    ]
    assert answer["capacity"]["ahead"] == []
    assert answer["capacity"]["text"] == (
        "a queue scan decides: the waiters ahead of it are tried first, and what they take is not"
        " counted here"
    )
    oracle.feed(_ev("ON_HOLD", "q"))
    answer = _decides(oracle, "q")
    assert answer["disposition"] == "held"
    assert _gates(answer) == [("hold", "deciding"), ("capacity", "context")]


def test_dl534_a_waiter_in_a_running_box_names_the_box_open() -> None:
    text = _R + _job("h", _NEEDS_R) + _box("B") + _job("m", _NEEDS_R + "box_name: B\n")
    oracle = _oracle(text, _ev("STARTJOB", "h"), _ev("STARTJOB", "B"))
    assert oracle.store.job["m"].status == "QUE_WAIT"
    answer = _decides(oracle, "m")
    assert answer["disposition"] == "short"
    assert answer["gates"][0]["text"] == "box 'B' is running"
    assert _gates(answer) == [("box", "open"), ("hold", "open"), ("capacity", "deciding")]


def test_dl534_a_waiter_s_failed_recheck_is_context_behind_capacity() -> None:
    """DL-257 under `queued-recheck=1`: the condition went false while the job waited; the scan
    that frees the unit would leave it unstarted, and while the unit is held the capacity
    decides."""
    text = _R + _job("h", _NEEDS_R) + _job("q", _NEEDS_R + 'condition: v(G) = "go"\n')
    events = (_ev("STARTJOB", "h"), _set("G", "go"), _set("G", "no", 1))
    oracle = _oracle(text, *events, switches={"queued-recheck": "1"})
    assert oracle.store.job["q"].status == "QUE_WAIT"
    answer = _decides(oracle, "q")
    assert answer["disposition"] == "short"
    assert answer["gates"][-1] == {
        "kind": "recheck",
        "state": "context",
        "text": "queued recheck fails: condition false",
        "job": None,
        "at": None,
    }


def test_dl534_a_queued_member_of_a_stopped_box_is_cancelled_by_the_scan() -> None:
    """DL-247: an admit-only scan keeps a member of a stopped box queued; a full scan cancels
    it."""
    text = _R + _job("h", _NEEDS_R) + _box("B") + _job("m", _NEEDS_R + "box_name: B\n")
    text += _job("n", "box_name: B\n")
    events = (_ev("STARTJOB", "h"), _ev("STARTJOB", "B"), _ev("KILLJOB", "B", 1))
    oracle = _oracle(text, *events)
    assert oracle.store.job["m"].status == "QUE_WAIT"
    assert oracle.store.job["B"].status != "RUNNING"
    answer = _decides(oracle, "m")
    assert (answer["disposition"], _deciding(answer)) == ("cancel", ["box"])


def _swapped(oracle: Oracle, text: str) -> None:
    """An update's swap: the oracle runs `text` from now on, and the queue scan it owes waits
    for the next input (`_wake_after_swap`)."""
    oracle.replace_catalog(
        lower_source(text), semantics=oracle.semantics, tz_aliases=None, default_tz=None
    )


def test_dl534_a_waiter_that_fits_alone_is_told_so_not_that_it_starts() -> None:
    """After an update raised R to two units and before the scan the swap owes, A and B each
    fit alone. The scan tries A first and B stays queued, so B's answer says it fits alone and
    names A ahead of it on R; it never says B starts."""
    two = "insert_resource: R\nres_type: R\namount: {}\n\n"
    jobs = _job("A", "resources: (R, QUANTITY=2, FREE=A)\n") + _job(
        "B", "resources: (R, QUANTITY=2, FREE=A)\n"
    )
    oracle = _oracle(two.format(1) + jobs, _ev("STARTJOB", "A"), _ev("STARTJOB", "B"))
    assert {oracle.store.job[j].status for j in "AB"} == {"QUE_WAIT"}
    _swapped(oracle, two.format(2) + jobs)
    first, second = _decides(oracle, "A"), _decides(oracle, "B")
    assert (first["disposition"], second["disposition"]) == ("fits", "fits")
    assert first["capacity"]["ahead"] == []
    assert second["capacity"]["ahead"] == [{"job": "A", "buckets": ["r:R"]}]
    assert ("capacity", "open") in _gates(second)
    assert second["gates"][-1]["text"] == "fits alone now"
    assert _outcome(oracle, "A", first) == "admit"
    assert _outcome(oracle, "B", second) == "keep"


def test_dl534_a_waiter_whose_recheck_fails_after_a_swap_leaves() -> None:
    """DL-257 under `queued-recheck=1`: Q's condition went false while it waited; an update
    then raised R, so Q fits, and the scan the swap owes leaves it unstarted. The open
    capacity gate precedes the deciding recheck."""
    jobs = _job("h", _NEEDS_R) + _job("q", _NEEDS_R + 'condition: v(G) = "go"\n')
    events = (_ev("STARTJOB", "h"), _set("G", "go"), _set("G", "no", 1))
    oracle = _oracle(_R + jobs, *events, switches={"queued-recheck": "1"})
    assert oracle.store.job["q"].status == "QUE_WAIT"
    _swapped(oracle, "insert_resource: R\nres_type: R\namount: 2\n\n" + jobs)
    answer = _decides(oracle, "q")
    assert answer["disposition"] == "leave"
    assert _gates(answer) == [("hold", "open"), ("capacity", "open"), ("recheck", "deciding")]
    assert answer["gates"][-1]["text"] == "queued recheck fails: condition false"


def test_dl534_a_waiter_that_fits_and_passes_its_recheck_names_both_open() -> None:
    jobs = _job("h", _NEEDS_R) + _job("q", _NEEDS_R)
    oracle = _oracle(
        _R + jobs, _ev("STARTJOB", "h"), _ev("STARTJOB", "q"), switches={"queued-recheck": "1"}
    )
    _swapped(oracle, "insert_resource: R\nres_type: R\namount: 2\n\n" + jobs)
    answer = _decides(oracle, "q")
    assert answer["disposition"] == "fits"
    assert answer["gates"][-2:] == [
        {"kind": "capacity", "state": "open", "text": "fits alone now", "job": None, "at": None},
        {
            "kind": "recheck",
            "state": "open",
            "text": "queued recheck passes",
            "job": None,
            "at": None,
        },
    ]


# ---------------------------------------------------------------- the corpus-seeds sample

#: Every this many applied steps of the `corpus-seeds` sample; the whole sample takes about two
#: minutes, this stride under thirty seconds.
_STRIDE = 7


def test_dl534_every_disposition_over_the_corpus_seeds_sample_is_the_oracle_s() -> None:
    """One sample of the golden harness's three, `corpus-seeds` (the corpus, the directed
    estates and seeds 0 to 199 at up to 50 jobs, default switches), at every seventh applied
    step, every job: `running` exactly when the row is STARTING or RUNNING; a start's
    disposition what a fork does with the attempt; a waiter's its isolated, unseeded
    readmission, and the full scan's too when no waiter ahead of it needs its buckets
    (`_agrees`). The read leaves the oracle's state as it was."""
    seen: set[str] = set()
    steps = 0
    for run in sample_runs("corpus-seeds"):
        oracle = Oracle(own(run.catalog), semantics=run.semantics)
        side = Side(oracle)
        for step in run.script:
            if isinstance(step, SealPoint):
                continue
            if side.apply(own(step)).error is not None:
                break
            steps += 1
            if steps % _STRIDE:
                continue
            before = _state(oracle)
            for job in oracle.catalog.jobs:
                answer = start_answer(oracle, job)
                seen.add(answer["disposition"])
                live = oracle.store.job[job].status in ("STARTING", "RUNNING")
                where = (run.label, step, job, answer)
                assert (answer["disposition"] == "running") == live, where
                if not live:
                    assert _agrees(oracle, job, answer), where
            assert _state(oracle) == before
    assert {
        "admit",
        "queue",
        "bypass",
        "defer",
        "skip",
        "ignored",
        "running",
        "short",
        "cancel",
    } <= seen


# ---------------------------------------------------------------- capacity


def _capacity(oracle: Oracle, job: str) -> dict[str, Any]:
    return _decides(oracle, job)["capacity"]


def test_dl534_several_holders_across_two_buckets_are_collected() -> None:
    text = (
        "insert_resource: R\nres_type: R\namount: 2\n\n"
        "insert_resource: S\nres_type: R\namount: 1\n\n"
        + _job("h1", "resources: (R, QUANTITY=1, FREE=A)\n")
        + _job("h2", "resources: (R, QUANTITY=1, FREE=A) AND (S, QUANTITY=1, FREE=A)\n")
        + _job("q", "resources: (R, QUANTITY=1, FREE=A) AND (S, QUANTITY=1, FREE=A)\n")
    )
    oracle = _oracle(text, _ev("STARTJOB", "h1"), _ev("STARTJOB", "h2"))
    short = _capacity(oracle, "q")["short"]
    assert [(s["bucket"], s["holders"]) for s in short] == [
        ("r:R", [{"job": "h1", "units": 1}, {"job": "h2", "units": 1}]),
        ("r:S", [{"job": "h2", "units": 1}]),
    ]


def test_dl534_spent_depletable_units_are_short_with_no_holder() -> None:
    text = (
        "insert_resource: D\nres_type: D\namount: 1\n\n"
        + _job("a", "resources: (D, QUANTITY=1)\n")
        + _job("b", "resources: (D, QUANTITY=1)\n")
    )
    oracle = _oracle(text, _ev("STARTJOB", "a"), _status("a", "SUCCESS"))
    [short] = _capacity(oracle, "b")["short"]
    assert (short["spent"], short["used"], short["holders"]) == (1, 1, [])


def test_dl534_a_threshold_below_the_demand_is_short_with_no_holder() -> None:
    text = "insert_resource: T\nres_type: T\namount: 1\n\n" + _job(
        "t", "resources: (T, QUANTITY=2)\n"
    )
    [short] = _capacity(_oracle(text), "t")["short"]
    assert short == {
        "bucket": "r:T",
        "capacity": 1,
        "demand": 2,
        "used": 0,
        "short": 1,
        "spent": 0,
        "holders": [],
    }


def test_dl534_units_left_on_a_failed_job_are_held_by_it() -> None:
    """DL-256: FREE=Y frees a renewable on SUCCESS only, so a failed run keeps its unit."""
    text = _R + _job("f", "resources: (R, QUANTITY=1, FREE=Y)\n") + _job("q", _NEEDS_R)
    oracle = _oracle(text, _ev("STARTJOB", "f"), _status("f", "FAILURE"))
    [short] = _capacity(oracle, "q")["short"]
    assert short["holders"] == [{"job": "f", "units": 1}]


_PRIORITY = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    "insert_resource: S\nres_type: R\namount: 1\n\n"
    + _job("h", "resources: (S, QUANTITY=1, FREE=A)\n")
    + _job("w", "resources: (R, QUANTITY=1, FREE=A) AND (S, QUANTITY=1, FREE=A)\npriority: 1\n")
    + _job("b", "resources: (R, QUANTITY=1, FREE=A)\npriority: 2\n")
    + _job("u", "resources: (R, QUANTITY=1, FREE=A)\npriority: 1\n")
)


def test_dl534_a_higher_priority_waiter_short_elsewhere_blocks_and_a_peer_does_not() -> None:
    """DL-255: `w` waits short of S and names R too, so it blocks `b`, of lower priority, on R
    though R is free; `u`, of the same priority as `w`, is not blocked."""
    oracle = _oracle(_PRIORITY, _ev("STARTJOB", "h"), _ev("STARTJOB", "w"))
    assert oracle.store.job["w"].status == "QUE_WAIT"
    blocked = _decides(oracle, "b")
    assert blocked["disposition"] == "queue"
    assert blocked["capacity"] == {
        "admissible": False,
        "blockers": [{"job": "w", "kind": "resource"}],
        "short": [],
    }
    free = _decides(oracle, "u")
    assert free["disposition"] == "admit"
    assert free["capacity"] == {"admissible": True, "blockers": [], "short": []}
    oracle.feed(_ev("ON_HOLD", "w", 1))  # a held waiter blocks no one (DL-247)
    assert _decides(oracle, "b")["capacity"]["blockers"] == []


_LOAD = (
    "insert_machine: m1\ntype: a\nnode_name: m1\nmax_load: 2\n\n"
    + _job("h", "machine: m1\njob_load: 2\n")
    + _job("w", "machine: m1\njob_load: 1\npriority: 1\n")
    + _job("b", "machine: m1\njob_load: 1\npriority: 2\n")
    + _job("z", "machine: m1\njob_load: 1\n")
)


def test_dl534_machine_load_names_its_holder_and_a_load_waiter_blocks() -> None:
    """DL-247: `h` holds the whole load; `w` waits for it and blocks `b`, of lower priority.
    `z` has no priority, so its start does not test the load and is admitted."""
    oracle = _oracle(_LOAD, _ev("STARTJOB", "h"), _ev("STARTJOB", "w"))
    capacity = _capacity(oracle, "b")
    assert capacity["blockers"] == [{"job": "w", "kind": "load"}]
    assert [(s["bucket"], s["holders"]) for s in capacity["short"]] == [
        ("m:m1", [{"job": "h", "units": 2}])
    ]
    answer = _decides(oracle, "z")
    assert answer["disposition"] == "admit"
    assert answer["capacity"]["short"] == []


def test_dl534_a_job_with_no_demand_reads_no_capacity_unless_the_pool_decides() -> None:
    """A positive-priority job with no load on a sized machine is still blocked by a load
    waiter: the capacity answer appears, with no short bucket."""
    text = _LOAD + _job("p", "machine: m1\npriority: 2\n")
    oracle = _oracle(text, _ev("STARTJOB", "h"), _ev("STARTJOB", "w"))
    answer = _decides(oracle, "p")
    assert answer["disposition"] == "queue"
    assert answer["capacity"] == {
        "admissible": False,
        "blockers": [{"job": "w", "kind": "load"}],
        "short": [],
    }
    assert ("capacity", "deciding") in _gates(answer)


def _crowded(waiters: int, fillers: int = 70) -> Oracle:
    """Jobs on one sized machine whose load fits, `fillers` of them idle: `h` holds R and S, `waiters` waiters
    of priority 1 want both, and `q`, of priority 5, wants R, so each waiter blocks it."""
    both = "resources: (R, QUANTITY=1, FREE=A) AND (S, QUANTITY=1, FREE=A)\n"
    load = "machine: m1\njob_load: 1\n"
    text = (
        "insert_machine: m1\ntype: a\nnode_name: m1\nmax_load: 1000\n\n"
        "insert_resource: R\nres_type: R\namount: 1\n\n"
        "insert_resource: S\nres_type: R\namount: 1\n\n"
        + _job("h", load + both)
        + _job("q", load + "priority: 5\n" + _NEEDS_R)
        + "".join(_job(f"w{i}", load + "priority: 1\n" + both) for i in range(waiters))
        + "".join(_job(f"f{i}", "machine: m1\n") for i in range(fillers))
    )
    events = [_ev("STARTJOB", "h"), *(_ev("STARTJOB", f"w{i}") for i in range(waiters))]
    return _oracle(text, *events)


def test_dl534_one_answer_is_linear_in_the_waiters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each blocker's own load stage is read off the machines' floors, computed once per
    answer, so the machine-entry checks grow by the same step for each added waiter."""
    from dsl41.capacity import CapacityPool

    checks = [0]
    entry = CapacityPool._machine_entry

    def counted(pool: CapacityPool, job_ir: Any) -> Any:
        checks[0] += 1
        return entry(pool, job_ir)

    found: dict[int, int] = {}
    for waiters in (32, 64, 128):
        oracle = _crowded(waiters)
        monkeypatch.setattr(CapacityPool, "_machine_entry", counted)
        checks[0] = 0
        answer = _explain(oracle, "q")
        found[waiters] = checks[0]
        monkeypatch.setattr(CapacityPool, "_machine_entry", entry)
        assert answer["disposition"] == "queue"
        assert len(answer["capacity"]["blockers"]) == waiters
    assert found[128] - found[64] == 2 * (found[64] - found[32])
    assert found == {32: 130, 64: 258, 128: 514}


def _kept(waiters: int, fillers: int = 70) -> Oracle:
    """`waiters` jobs of priority 1 that each kept one unit of R after their first run (FREE=N,
    DL-256) and wait again, behind `h`'s hold of S; `q`, of priority 5, wants R, so each of
    them blocks it while holding units of its own."""
    keep = "resources: (R, QUANTITY=1, FREE=N) AND (S, QUANTITY=1, FREE=A)\npriority: 1\n"
    text = (
        "insert_resource: R\nres_type: R\namount: 1000\n\n"
        "insert_resource: S\nres_type: R\namount: 1\n\n"
        + _job("h", "resources: (S, QUANTITY=1, FREE=A)\n")
        + _job("q", "priority: 5\nresources: (R, QUANTITY=1, FREE=A)\n")
        + "".join(_job(f"w{i}", keep) for i in range(waiters))
        + "".join(_job(f"f{i}") for i in range(fillers))
    )
    events: list[Event] = []
    for i in range(waiters):
        events += [_ev("STARTJOB", f"w{i}"), _status(f"w{i}", "SUCCESS")]
    events.append(_ev("STARTJOB", "h", 1))
    events += [_ev("STARTJOB", f"w{i}", 1) for i in range(waiters)]
    return _oracle(text, *events)


class _Counted(Mapping[str, Any]):
    """The store's rows, counting each row a read takes: an item an iteration yields, or one
    looked up."""

    def __init__(self, rows: Mapping[str, Any], tally: list[int]) -> None:
        self._rows, self._tally = rows, tally

    def __getitem__(self, key: str) -> Any:
        self._tally[0] += 1
        return self._rows[key]

    def __iter__(self) -> Iterator[str]:
        for key in self._rows:
            self._tally[0] += 1
            yield key

    def __len__(self) -> int:
        return len(self._rows)

    def items(self) -> Iterator[tuple[str, Any]]:  # type: ignore[override]
        for item in self._rows.items():
            self._tally[0] += 1
            yield item


def _visits(monkeypatch: pytest.MonkeyPatch, oracle: Oracle, job: str) -> tuple[int, Any]:
    """The rows one answer for `job` visits, and the answer."""
    from dsl41.oracle_state import RuntimeState

    tally = [0]
    rows = RuntimeState.job
    with monkeypatch.context() as patch:
        patch.setattr(
            RuntimeState, "job", property(lambda store: _Counted(rows.fget(store), tally))
        )
        answer = start_answer(oracle, job)
    return tally[0], answer


@pytest.mark.parametrize("workload", ["load", "kept"])
def test_dl534_one_answer_visits_each_row_a_bounded_number_of_times(
    monkeypatch: pytest.MonkeyPatch, workload: str
) -> None:
    """The whole work of one answer, counted in row visits, grows by the same step for each
    added waiter, for `q`'s start and for the last waiter's readmission, whose `ahead` also
    sorts the waiters. Waiters that hold units of their own get the one usage total less
    their reservations (`kept`)."""
    build = _crowded if workload == "load" else _kept
    found: dict[str, dict[int, int]] = {"q": {}, "last": {}}
    for waiters in (32, 64, 128):
        oracle = build(waiters)
        visits, answer = _visits(monkeypatch, oracle, "q")
        assert answer["disposition"] == "queue"
        pool, q = oracle._pool, oracle.catalog.jobs["q"]
        live = pool.resource_blockers(
            q, oracle.store.job, oracle.store.consumed, oracle._counts_as_waiter
        )
        assert [b["job"] for b in answer["capacity"]["blockers"]] == list(live)  # the oracle's own
        found["q"][waiters] = visits
        visits, answer = _visits(monkeypatch, oracle, f"w{waiters - 1}")
        assert answer["attempt"] == "readmit"
        assert len(answer["capacity"]["ahead"]) == waiters - 1
        found["last"][waiters] = visits
    for counts in found.values():
        assert counts[128] - counts[64] == 2 * (counts[64] - counts[32]), found
    assert found == _VISITS[workload]


#: The row visits measured (`test_dl534_one_answer_visits_each_row_a_bounded_number_of_times`).
_VISITS = {
    "load": {"q": {32: 833, 64: 1089, 128: 1601}, "last": {32: 937, 64: 1225, 128: 1801}},
    "kept": {"q": {32: 625, 64: 817, 128: 1201}, "last": {32: 833, 64: 1089, 128: 1601}},
}


# ---------------------------------------------------------------- wake


def _engine(text: str, *, scheduler: bool = False) -> Engine:
    catalog = lower_source(text)
    return Engine(
        catalog,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=Scheduler(catalog, start=T0) if scheduler else None,
    )


def _served(engine: Engine, job: str, tmp_path: Path | None = None) -> dict[str, Any]:
    """`explain` as the control server answers it, with the oracle left as it was."""
    server = ControlServer(engine, (tmp_path or Path("/nonexistent")) / "control.sock")
    before = _state(engine.oracle)
    rows = dict(engine.oracle.store.job)
    answer = server._explain({"job": job})
    assert _state(engine.oracle) == before and dict(engine.oracle.store.job) == rows
    json.dumps(answer)  # a wire value
    return answer


def _kinds(answer: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(entry["kind"], entry["at"]) for entry in answer["wake"]]


def test_dl534_a_dated_job_s_wake_is_its_next_tick_from_the_scheduler() -> None:
    text = _job("d", 'date_conditions: 1\ndays_of_week: all\nstart_times: "09:30"\n')
    engine = _engine(text, scheduler=True)
    answer = _served(engine, "d")
    assert _kinds(answer) == [("tick", datetime(2026, 7, 1, 9, 30).isoformat())]


def test_dl534_a_dated_job_whose_only_time_is_a_window_has_no_tick() -> None:
    """No tick, and the date gate's text holds for it too: inside its window an operator's
    STARTJOB passes the gate and runs it."""
    text = _job("w", 'date_conditions: 1\nrun_window: "10:00-11:00"\n')
    answer = _served(_engine(text, scheduler=True), "w")
    assert _kinds(answer) == [("tick", None), ("operator", None)]
    assert answer["wake"][0]["text"] == "none scheduled"
    assert _deciding(answer) == ["date"]
    oracle = _oracle(text)
    oracle.advance(_at(150))  # 10:30, inside the window
    assert _decides(oracle, "w")["gates"][2]["text"] == UNARMED
    oracle.feed(_ev("STARTJOB", "w", 151))
    assert oracle.store.job["w"].status == "RUNNING"


def test_dl534_a_tick_the_scheduler_owes_is_named_after_the_schedule_is_removed() -> None:
    """spec-reload: an update that removes a job's schedule at its due tick leaves the tick
    owed, and the scheduler fires it, so the wake names it."""
    dated = _job("d", 'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n')
    plain = _job("d")
    scheduler = Scheduler(lower_source(dated), start=T0)
    nine = datetime(2026, 7, 1, 9, 0)
    scheduler.replace(
        lower_source(plain),
        added=(),
        changed=("d",),
        removed=(),
        now=nine,
        default_tz=None,
        tz_aliases=None,
        semantics=None,
    )
    assert scheduler.next_tick("d") == nine
    oracle = _oracle(plain)
    assert wake(oracle, "d", scheduler.next_tick("d")) == [
        {"kind": "tick", "text": "its next scheduled tick", "job": None, "at": nine.isoformat()}
    ]
    assert [event.payload["job"] for event in scheduler.pop_due(nine)] == ["d"]


def test_dl534_a_condition_only_job_and_a_job_with_nothing() -> None:
    text = _job("c", "condition: s(a)\n") + _job("a")
    engine = _engine(text)
    assert _kinds(_served(engine, "c")) == [("condition", None)]
    assert _served(engine, "a")["wake"] == [
        {"kind": "operator", "text": "only an operator's start", "job": None, "at": None}
    ]


def test_dl534_a_box_member_is_woken_by_its_box_start() -> None:
    answer = _served(_engine(_box("B") + _job("m", "box_name: B\n")), "m")
    assert answer["wake"] == [{"kind": "box", "text": "box 'B' starts", "job": "B", "at": None}]


def test_dl534_a_deferred_start_is_a_wake() -> None:
    oracle = _armed(30)
    oracle.feed(_set("G", "go", 31))  # the wake defers to 10:00
    assert [entry["kind"] for entry in wake(oracle, "d", None)] == ["tick", "deferred", "condition"]


def test_dl534_the_same_value_set_again_starts_a_completed_job_as_wake_said() -> None:
    """A condition wake needs an update while the condition is true, not a false-to-true
    edge: `J` completed on `G = go`, and setting `go` again starts its second run."""
    oracle = _oracle(_job("J", 'condition: v(G) = "go"\n'), _set("G", "go"))
    oracle.feed(_status("J", "SUCCESS", 1))
    answer = _decides(oracle, "J")
    assert answer["disposition"] == "admit"
    assert [entry["kind"] for entry in wake(oracle, "J", None)] == ["condition"]
    oracle.feed(_set("G", "go", 2))
    row = oracle.store.job["J"]
    assert (row.status, row.run_number) == ("RUNNING", 2)


# ---------------------------------------------------------------- dispatch


def test_dl534_a_healthy_running_job_reports_no_dispatch_wait(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = start_run(
            lower_source(_job("a")),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev("STARTJOB", "a"))
        await engine.run_until_quiescent(T0)
        answer = _served(engine, "a")
        assert answer["disposition"] == "running" and "dispatch" not in answer
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())


def test_dl534_a_running_file_watch_reports_what_it_watches_and_nothing_seen(
    tmp_path: Path,
) -> None:
    """DL-68: a live watch has launched; the answer gives its path and interval as `status`
    does, and nothing about whether the file was seen."""
    from dsl41.runner_adapters import FileWatcherAdapter

    target = tmp_path / "never"
    text = f"insert_job: f\njob_type: f\nwatch_file: {target}\nwatch_interval: 60\n"

    async def scenario() -> None:
        engine = start_run(
            lower_source(text),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter(existence="stable")},  # type: ignore[dict-item]
        )
        engine.inject(_ev("STARTJOB", "f"))
        await engine.run_until_quiescent(T0)
        answer = _served(engine, "f")
        assert answer["disposition"] == "running"
        assert answer["watching"] == {"file": str(target), "interval": 60, "min_size": None}
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())


def test_dl534_a_start_behind_an_earlier_attempt_s_cleanup(tmp_path: Path) -> None:
    """DL-235, DL-426: an operator's INACTIVE leaves run 1's attempt live; the next start's
    run 2 waits until that attempt, being stopped, settles."""

    async def scenario() -> None:
        engine = start_run(
            lower_source("insert_job: j\njob_type: c\ncommand: sleep 30\n"),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev("STARTJOB", "j"))
        await engine.run_until_quiescent(T0)
        _adopt(engine)
        spawn = {"command_pgid": 2**22 + 7, "command_pid": 2**22 + 7, "command_start_time": "t"}
        engine.attempts.spawn_recorded(RUN_ID, spawn)
        engine.attempts.stop(RUN_ID, "engine_stop")
        engine.inject(_status("j", "INACTIVE"))
        engine.inject(_ev("STARTJOB", "j"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["j"].run_number == 2
        answer = _served(engine, "j")
        assert answer["disposition"] == "running"
        assert answer["dispatch"] == {
            "run_number": 2,
            "kind": "start",
            "wait": "cleanup",
            "attempt": RUN_ID,
            "token": None,
            "removes": False,
        }
        # a drain keeps it back too, for the restart to launch (spec-drain)
        engine.request_stop()
        await engine.run_until_quiescent(T0)
        assert engine.draining
        assert _served(engine, "j")["dispatch"]["wait"] == "drain"
        engine.stops.cancel_all()
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())


def test_dl534_a_replacement_parked_behind_its_own_attempt_s_ladder_names_that_attempt(
    tmp_path: Path,
) -> None:
    """DL-426: a plan's replacement waits while the replaced attempt's stop ladder runs
    (`perform_plan`); the wait is that attempt's cleanup, named. The plan is the one an exit 1
    under `n_retrys` chooses, journaled as the engine journals it. The live sequence (a retry
    whose command exits while its child is on the ladder) is
    `test_live_engine_branches.test_retry_waits_for_cleanup_then_update_releases_replacement`,
    which runs on Linux only."""
    from dsl41.runner import _Plan, plan_record

    async def scenario() -> None:
        engine = start_run(
            lower_source("insert_job: j\njob_type: c\ncommand: sleep 30\nn_retrys: 1\n"),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev("STARTJOB", "j"))
        await engine.run_until_quiescent(T0)
        _adopt(engine)
        spawn = {"command_pgid": 2**22 + 7, "command_pid": 2**22 + 7, "command_start_time": "t"}
        engine.attempts.spawn_recorded(RUN_ID, spawn)
        engine.attempts.stop(RUN_ID, "engine_stop")
        assert engine.attempts.ladder_running(RUN_ID)
        step = {
            "step": "dispatch",
            "attempt": "6c2b9a1e-0d3f-4e5a-9b8c-7d6e5f4a3b2c",
            "ordinal": 2,
            "env": {**engine.dispatches["j"]["env"], "DSL41_ATTEMPT": "2"},
            "after": "exit 1",
        }
        assert engine.journal is not None
        seq = engine.journal.append(plan_record(RUN_ID, "j", 1, "rerun", [step], T0))
        engine._plans[RUN_ID] = _Plan(RUN_ID, "j", 1, [step], seq)
        engine.perform_plan(RUN_ID)
        assert RUN_ID in engine._waiting_plans
        assert engine.blocked("j", RUN_ID) is None  # `blocked` skips the replaced attempt
        answer = _served(engine, "j")
        assert answer["dispatch"] == {
            "run_number": 1,
            "kind": "replacement",
            "wait": "cleanup",
            "attempt": RUN_ID,
            "token": None,
            "removes": False,
        }
        engine.stops.cancel_all()
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())


#: `explain`'s statement while a restart recovers (DL-534).
RECOVERING = (
    "the engine is recovering from a restart: the starts it owes are not all known until"
    " recovery ends, so this answer gives no dispatch evidence"
)


def _recovering(monkeypatch: pytest.MonkeyPatch, job: str) -> list[dict[str, Any]]:
    """The answers for `job` read as a restart's recovery ends, just before its owed dispatches
    go (`Engine._finish_recovery`)."""
    seen: list[dict[str, Any]] = []
    finish = Engine._finish_recovery

    def finish_recovery(engine: Engine) -> None:
        seen.append(_served(engine, job))
        finish(engine)

    monkeypatch.setattr(Engine, "_finish_recovery", finish_recovery)
    return seen


def _recovery_says_so(answer: dict[str, Any]) -> None:
    assert answer["disposition"] == "running"
    assert answer["recovering"] == RECOVERING
    assert "dispatch" not in answer


def test_dl534_a_start_during_a_drain_waits_for_it_and_a_restart_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec-drain: an engine-made start decided while the engine drains waits as
    `dispatch_pending` for the restart. While the restart recovers, the answer says so and
    makes no dispatch claim; once it has recovered and launched the start, it names none."""
    text = _job("a") + _job("b")
    root = tmp_path / "run"
    seen = _recovering(monkeypatch, "b")

    async def scenario() -> None:
        adapters = {"CMD": FakeAdapter(script={("a", 1): (60.0, 0)})}
        engine = start_run(
            lower_source(text), root, clock=VirtualClock(start=T0), adapters=adapters
        )
        engine.inject(_ev("STARTJOB", "a"))
        await engine.run_until_quiescent(T0)
        engine.request_stop()
        engine.inject(_ev("STARTJOB", "b"), source=None)  # the engine's own, as a tick is
        await engine.run_until_quiescent(_at(5))
        assert engine.draining
        drained = _served(engine, "b")
        assert drained["dispatch"] == {
            "run_number": 1,
            "kind": "start",
            "wait": "drain",
            "attempt": None,
            "token": None,
            "removes": False,
        }
        assert "recovering" not in drained
        # `a`'s start was dispatched before the drain: nothing is owed for it
        assert "dispatch" not in _served(engine, "a")
        await engine.shutdown()
        _close(engine)
        restarted = await resume_run(
            lower_source(text), root, clock=VirtualClock(start=_at(6)), adapters=adapters
        )
        assert restarted.recovery is not None
        _recovery_says_so(_served(restarted, "b"))
        await restarted.run_until_quiescent(_at(7))
        [queued] = seen
        _recovery_says_so(queued)
        after = _served(restarted, "b")
        assert "dispatch" not in after and "recovering" not in after
        await restarted.shutdown()
        _close(restarted)

    asyncio.run(scenario())


def test_dl534_a_start_decided_with_no_dispatch_record_is_not_called_settled_in_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-102's crash cut: the engine committed STARTJOB's decision and died before any
    dispatch record. The restart owes the start with no record of it until its catch-up
    writes one, so through the whole recovery the answer says it is recovering and claims no
    dispatch, never a settled `running`."""
    from store_surgery import rows

    root = tmp_path / "run"
    seen = _recovering(monkeypatch, "a")

    async def scenario() -> None:
        adapters = {"CMD": FakeAdapter(default=None)}
        engine = start_run(
            lower_source(_job("a")), root, clock=VirtualClock(start=T0), adapters=adapters
        )
        engine._dispatch = lambda emitted, selected=None: None  # type: ignore[method-assign]
        engine.inject(_ev("STARTJOB", "a"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["a"].status == "RUNNING"
        await engine.shutdown()
        _close(engine)
        kinds = {kind for _seq, kind, _body in rows(root)}
        assert "dispatch" not in kinds and "dispatch_pending" not in kinds
        restarted = await resume_run(
            lower_source(_job("a")), root, clock=VirtualClock(start=_at(4)), adapters=adapters
        )
        _recovery_says_so(_served(restarted, "a"))
        await restarted.run_until_quiescent(_at(5))
        [queued] = seen
        _recovery_says_so(queued)
        assert "dispatch" in {kind for _seq, kind, _body in rows(root)}
        after = _served(restarted, "a")
        assert "dispatch" not in after and "recovering" not in after
        await restarted.shutdown()
        _close(restarted)

    asyncio.run(scenario())


def _cmd(condition: str | None = None, command: str = "sleep 30") -> str:
    return f"job_type: c\ncommand: {command}\n" + (f"condition: {condition}\n" if condition else "")


@pytest.mark.parametrize("removed", [False, True])
def test_dl534_a_start_an_update_s_filter_holds_names_its_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, removed: bool
) -> None:
    """DL-527: while an update waits for `c`'s stop, the start decided for `m`, a job it changes
    or removes, waits for the update; the reply names its token and whether it removes `m`."""
    old = f"insert_job: c\n{_cmd()}\ninsert_job: m\n{_cmd('v(GO) = "go"')}\n"
    new = f"insert_job: c\n{_cmd(command='sleep 31')}\n"
    if not removed:
        new += f"insert_job: m\n{_cmd('v(GO) = "go"', 'sleep 32')}\n"

    async def scenario() -> None:
        ladders = _Ladders(monkeypatch)
        ladders.alive.add(PGID)
        engine = _start(tmp_path, old, ())
        try:
            engine.inject(_ev("STARTJOB", "c"))
            await engine.run_until_quiescent(T0)
            _observed(engine, "c", PGID)
            _estate(tmp_path, new, ())
            answer = await _request(engine, _plan(engine, tmp_path))
            assert isinstance(answer.decision, Admitted)
            await _started(ladders, PGID)
            engine.inject(_set("GO", "go"))
            await engine.run_until_quiescent(T0)
            assert engine.update.stopping
            served = _served(engine, "m")
            assert served["disposition"] == "running"
            assert served["dispatch"] == {
                "run_number": 1,
                "kind": "start",
                "wait": "update",
                "attempt": None,
                "token": answer.decision.token,
                "removes": removed,
            }
        finally:
            _teardown(engine, ladders)
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())


# ---------------------------------------------------------------- safety


def test_dl534_a_fresh_engine_answers_a_held_windowed_job_without_advancing() -> None:
    """Before its first input the oracle has no instant; the answer reads a fork at the
    engine's present one, and the window, below the hold, is read there."""
    text = _job("w", 'date_conditions: 1\nrun_window: "10:00-11:00"\nstatus: on_hold\n')
    engine = _engine(text)
    assert engine.oracle._now is None
    answer = _served(engine, "w")
    assert engine.oracle._now is None
    assert answer["as_of"] == T0.isoformat()
    assert _gates(answer)[:4] == [
        ("ice", "open"),
        ("hold", "deciding"),
        ("date", "context"),
        ("window", "context"),
    ]
    assert reading(engine.oracle, T0) is not engine.oracle


def test_dl534_explain_on_a_name_outside_the_catalog_creates_no_row() -> None:
    engine = _engine(_job("a"))
    rows = dict(engine.oracle.store.job)
    answer = ControlServer(engine, Path("/nonexistent"))._explain({"job": "ghost"})
    assert answer["ok"] is False and answer["code"] == "unknown_job"
    assert dict(engine.oracle.store.job) == rows


# ---------------------------------------------------------------- atoms and deps

_ATOMS = (
    _job("a")
    + _job(
        "j",
        "condition: n(j) & s(a, 01.00) & s(ghost) & s(x^OTH) & v(G) = go & n(a)\n",
    )
    + "insert_xinst: OTH\nxtype: a\nxmachine: oth\n\n"
)


def test_dl534_each_atom_carries_its_identity() -> None:
    engine = _engine(_ATOMS)
    engine.oracle.advance(T0)
    atoms = {entry["atom"]: entry for entry in _served(engine, "j")["atoms"]}
    flags = ("job", "kind", "lookback", "guard", "self", "undefined", "external")

    def of(atom: str) -> tuple[Any, ...]:
        return tuple(atoms[atom][flag] for flag in flags)

    assert of("n(j)") == ("j", "status", None, True, True, False, False)
    assert of("s(a, 01.00)") == ("a", "status", "01.00", False, False, False, False)
    assert of("s(ghost)") == ("ghost", "status", None, False, False, True, False)
    assert of("s(x^OTH)") == ("x^OTH", "status", None, False, False, False, True)
    assert of("v(G) = go") == (None, "global", None, False, False, False, False)
    assert of("n(a)") == ("a", "status", None, True, False, False, False)
    assert atoms["v(G) = go"]["actual"] is None


def _legacy_explain(oracle: Oracle, job: str) -> dict[str, Any]:
    """The `explain` answer as it was before DL-534."""
    from dsl41.conditions import GlobalAtom, iter_atoms
    from dsl41.dsl import cond_to_source

    attr = oracle.catalog.jobs[job].sem.condition
    if attr is None:
        return {"ok": True, "job": job, "condition": None, "satisfied": True, "atoms": []}
    atoms = []
    for atom in iter_atoms(attr.cond):
        entry: dict[str, Any] = {"atom": cond_to_source(atom), "true": oracle._cond_true(atom, job)}
        if isinstance(atom, GlobalAtom):
            entry["actual"] = oracle.store.global_value(atom.name)
        atoms.append(entry)
    return {
        "ok": True,
        "job": job,
        "condition": cond_to_source(attr.cond),
        "satisfied": oracle._cond_true(attr.cond, job),
        "atoms": atoms,
    }


def test_dl534_the_legacy_explain_and_deps_fields_are_unchanged_over_the_corpus() -> None:
    from oracle_inputs import corpus_jobs

    for corpus in corpus_jobs(scripts=1, length=10):
        engine = Engine(
            own(corpus.catalog), clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
        )
        for step in corpus.script:
            if isinstance(step, Event):
                try:
                    engine.oracle.feed(own(step))
                except Exception:  # noqa: BLE001 -- the harness puts back what raises
                    break
        server = ControlServer(engine, Path("/nonexistent"))
        for job in engine.oracle.catalog.jobs:
            answer = server._explain({"job": job})
            legacy = {key: answer[key] for key in ("ok", "job", "condition", "satisfied")}
            legacy["atoms"] = [
                {key: atom[key] for key in ("atom", "true", "actual") if key in atom}
                for atom in answer["atoms"]
            ]
            assert json.dumps(legacy, sort_keys=True) == json.dumps(
                _legacy_explain(engine.oracle, job), sort_keys=True
            )
            deps = server._deps({"job": job})
            atoms = deps.pop("upstream_atoms")
            assert [atom["atom"] for atom in atoms] == [atom["atom"] for atom in answer["atoms"]]
            assert sorted(deps) == [
                "box_name",
                "downstream",
                "globals",
                "job",
                "members",
                "ok",
                "upstream",
            ]
            assert set(deps["upstream"]) == {atom["job"] for atom in atoms if atom["job"]}
