"""Synthetic witnesses of public predicate, box, resource and timer rules."""

from collections import Counter
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from functools import lru_cache

import pytest
from bisim_harness import EngineHarness

from dsl41.ast_jil import parse, render, render_canonical
from dsl41.autocal import CalendarRuleError, compile_calendar
from dsl41.ir import CatalogIR, lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event
from dsl41.runner_scheduler import Scheduler
from dsl41.semantics import resolve as resolve_switches

Runtime = Oracle | EngineHarness
T0 = datetime(2026, 2, 2, 8)


def job(name: str, kind: str = "c", extra: str = "") -> str:
    execution = "command: :\nmachine: inert_node\n" if kind == "c" else ""
    if kind == "f":
        execution = "machine: inert_node\nwatch_file: /synthetic/marker\nwatch_interval: 3\n"
    return f"insert_job: {name}\njob_type: {kind}\n{execution}{extra}\n"


def resource(name: str) -> str:
    return f"insert_resource: {name}\nres_type: R\namount: 1\n\n"


def demand(*names: str) -> str:
    return "resources: " + " AND ".join(f"({n}, QUANTITY=1, FREE=A)" for n in names) + "\n"


@lru_cache(maxsize=None)
def catalog(text: str) -> CatalogIR:
    """F1/F2 apply to every distinct inline source before strict lowering."""
    ast = parse(text)
    assert render(ast) == text
    canonical = render_canonical(ast)
    assert render_canonical(parse(canonical)) == canonical
    return lower_source(text)


@pytest.fixture(params=("oracle", "engine"))
def runtime_factory(request: pytest.FixtureRequest) -> Iterator[Callable[..., Runtime]]:
    instances: list[Runtime] = []

    def make(text: str, switches: dict[str, str] | None = None) -> Runtime:
        cls = Oracle if request.param == "oracle" else EngineHarness
        runtime = cls(catalog(text), semantics=resolve_switches(switches or {}))
        instances.append(runtime)
        return runtime

    try:
        yield make
    finally:
        for runtime in reversed(instances):
            if isinstance(runtime, EngineHarness):
                runtime.close()


def feed(runtime: Runtime, minute: float, kind: str, name: str, **payload: str) -> None:
    runtime.feed(
        Event(at=T0 + timedelta(minutes=minute), kind=kind, payload={"job": name, **payload})
    )


def end(runtime: Runtime, minute: float, name: str, status: str = "SUCCESS") -> None:
    feed(runtime, minute, "STATUS", name, status=status)


def assert_runs(runtime: Runtime, **expected: int) -> None:
    """Every STARTING is counted; ordinal starts identify each job's run."""
    counts = Counter(e.job for e in runtime.trace() if e.transition.endswith("->STARTING"))
    assert dict(counts) == {name: count for name, count in expected.items() if count}
    assert {name: row.run_number for name, row in runtime.store.job.items()} == {
        name: expected.get(name, 0) for name in runtime.store.job
    }
    for name, count in expected.items():
        assert list(range(1, counts[name] + 1)) == list(range(1, count + 1))


def assert_states(runtime: Runtime, **expected: str) -> None:
    assert {name: runtime.store.job[name].status for name in expected} == expected


def assert_units(runtime: Runtime, **expected: int) -> None:
    units: Counter[str] = Counter()
    for row in runtime.store.job.values():
        for reservation in row.reservations:
            units[reservation.bucket] += reservation.units
    assert dict(units) == expected
    assert all(value <= 1 for value in units.values())


RING = (
    job("read_stage", "b", "condition: d(work_stage,0)\nstatus: ON_HOLD\n")
    + job("read_marker", "f", "box_name: read_stage\nstatus: ON_HOLD\n")
    + job("work_stage", "b", "condition: s(read_stage)\nstatus: ON_HOLD\n")
    + job("work_task", extra="box_name: work_stage\nstatus: ON_HOLD\n")
)


def release(runtime: Runtime, minute: float) -> None:
    for index, name in enumerate(sorted(runtime.store.job)):
        feed(runtime, minute + index / 100, "OFF_HOLD", name)


def test_sem04_cold_ring_needs_prior_completion(runtime_factory: Callable[..., Runtime]) -> None:
    runtime = runtime_factory(RING)
    assert all(row.on_hold for row in runtime.store.job.values())
    feed(runtime, 0, "STARTJOB", "read_stage")
    feed(runtime, 0, "STARTJOB", "work_stage")
    assert_runs(runtime)
    release(runtime, 1)
    assert all(not row.on_hold for row in runtime.store.job.values())
    assert_states(runtime, **dict.fromkeys(runtime.store.job, "INACTIVE"))
    assert_runs(runtime)


@pytest.mark.parametrize("outcome", ("SUCCESS", "FAILURE"))
def test_sem04_retained_completion_rearms_ring(
    runtime_factory: Callable[..., Runtime], outcome: str
) -> None:
    runtime = runtime_factory(RING)
    end(runtime, 0, "work_stage", outcome)
    feed(runtime, 0.1, "ON_HOLD", "work_stage")
    assert all(row.on_hold for row in runtime.store.job.values())
    assert_states(runtime, work_stage=outcome)
    assert runtime.store.job["work_stage"].last_end_at == T0
    assert runtime.store.job["read_stage"].last_end_at is None
    assert_runs(runtime)
    release(runtime, 1)
    assert_states(
        runtime,
        read_stage="RUNNING",
        read_marker="RUNNING",
        work_stage=outcome,
        work_task="INACTIVE",
    )
    assert_runs(runtime, read_stage=1, read_marker=1)
    for cycle in (1, 2):
        end(runtime, cycle * 4, "read_marker")
        assert_states(
            runtime,
            read_stage="SUCCESS",
            read_marker="SUCCESS",
            work_stage="RUNNING",
            work_task="RUNNING",
        )
        assert_runs(runtime, read_stage=cycle, read_marker=cycle, work_stage=cycle, work_task=cycle)
        end(runtime, cycle * 4 + 1, "work_task", outcome)
        assert_states(
            runtime,
            read_stage="RUNNING",
            read_marker="RUNNING",
            work_stage=outcome,
            work_task=outcome,
        )
        assert_runs(
            runtime, read_stage=cycle + 1, read_marker=cycle + 1, work_stage=cycle, work_task=cycle
        )


@pytest.mark.parametrize("outcome", ("SUCCESS", "FAILURE"))
def test_sem04_old_completion_cannot_rearm_read_stage(
    runtime_factory: Callable[..., Runtime], outcome: str
) -> None:
    runtime = runtime_factory(RING)
    end(runtime, 0, "work_stage", outcome)
    feed(runtime, 0.1, "ON_HOLD", "work_stage")
    end(runtime, 1, "read_stage")
    feed(runtime, 1.1, "ON_HOLD", "read_stage")
    assert all(row.on_hold for row in runtime.store.job.values())
    assert runtime.store.job["work_stage"].last_end_at == T0
    assert runtime.store.job["read_stage"].last_end_at == T0 + timedelta(minutes=1)
    release(runtime, 2)
    assert_states(
        runtime,
        read_stage="SUCCESS",
        read_marker="INACTIVE",
        work_stage="RUNNING",
        work_task="RUNNING",
    )
    assert_runs(runtime, work_stage=1, work_task=1)
    end(runtime, 3, "work_task", outcome)
    assert_states(
        runtime, read_stage="RUNNING", read_marker="RUNNING", work_stage=outcome, work_task=outcome
    )
    assert_runs(runtime, read_stage=1, read_marker=1, work_stage=1, work_task=1)


def gate_source(two: bool = False) -> str:
    condition = "s(ready,0) & n(peer)" + (" & s(other_ready,0)" if two else "")
    return (
        job("ready")
        + (job("other_ready") if two else "")
        + job("peer", "b")
        + job("peer_task", extra="box_name: peer\n")
        + job("gate_box", "b")
        + job("gate_sentinel", extra="box_name: gate_box\n")
        + job("consumer", extra=f"box_name: gate_box\ncondition: {condition}\n")
    )


@pytest.mark.parametrize("peer_end", ("SUCCESS", "FAILURE", "KILLJOB"))
@pytest.mark.parametrize("two", (False, True), ids=("one-success", "two-successes"))
def test_sem02_notrunning_and_zero_lookback_gate(
    runtime_factory: Callable[..., Runtime], peer_end: str, two: bool
) -> None:
    runtime = runtime_factory(gate_source(two))
    feed(runtime, 0, "STARTJOB", "peer")
    feed(runtime, 0, "STARTJOB", "gate_box")
    end(runtime, 1, "ready")
    if two:
        end(runtime, 1, "other_ready")
    assert_states(runtime, consumer="INACTIVE", peer="RUNNING")
    assert_runs(runtime, peer=1, peer_task=1, gate_box=1, gate_sentinel=1)
    if peer_end == "KILLJOB":
        feed(runtime, 2, "KILLJOB", "peer")
    else:
        end(runtime, 2, "peer_task", peer_end)
    assert_states(
        runtime, peer="TERMINATED" if peer_end == "KILLJOB" else peer_end, consumer="RUNNING"
    )
    end(runtime, 2.1, "ready")
    assert_runs(runtime, peer=1, peer_task=1, gate_box=1, gate_sentinel=1, consumer=1)
    end(runtime, 3, "consumer")
    end(runtime, 3.1, "peer_task", "FAILURE")
    assert_states(runtime, gate_box="RUNNING", consumer="SUCCESS")
    assert_runs(runtime, peer=1, peer_task=1, gate_box=1, gate_sentinel=1, consumer=1)
    end(runtime, 3.2, "gate_sentinel")
    assert_states(runtime, gate_box="SUCCESS")
    feed(runtime, 4, "STARTJOB", "gate_box")
    assert_states(runtime, consumer="INACTIVE")
    end(runtime, 5, "ready")
    if two:
        assert_states(runtime, consumer="INACTIVE")
        assert_runs(runtime, peer=1, peer_task=1, gate_box=2, gate_sentinel=2, consumer=1)
        end(runtime, 6, "other_ready")
    assert_states(runtime, consumer="RUNNING")
    assert_runs(runtime, peer=1, peer_task=1, gate_box=2, gate_sentinel=2, consumer=2)


def test_sem02_queued_peer_is_not_a_mutex(runtime_factory: Callable[..., Runtime]) -> None:
    source = (
        resource("unit")
        + job("holder", extra=demand("unit"))
        + gate_source().replace(
            "insert_job: peer\njob_type: b\n", "insert_job: peer\njob_type: b\n" + demand("unit")
        )
    )
    runtime = runtime_factory(source)
    feed(runtime, 0, "STARTJOB", "holder")
    feed(runtime, 0, "STARTJOB", "peer")
    feed(runtime, 0, "STARTJOB", "gate_box")
    end(runtime, 1, "ready")
    assert_states(
        runtime, holder="RUNNING", peer="QUE_WAIT", peer_task="INACTIVE", consumer="RUNNING"
    )
    assert runtime.store.job["peer"].reservations == ()
    assert_units(runtime, **{"r:unit": 1})
    assert_runs(runtime, holder=1, gate_box=1, gate_sentinel=1, consumer=1)
    end(runtime, 2, "holder")
    assert_states(runtime, peer="RUNNING", peer_task="RUNNING", consumer="RUNNING")
    assert_units(runtime, **{"r:unit": 1})
    assert_runs(runtime, holder=1, peer=1, peer_task=1, gate_box=1, gate_sentinel=1, consumer=1)


SCHEDULED = (
    resource("amber")
    + resource("blue")
    + job("amber_holder", extra=demand("amber"))
    + job("blue_holder", extra=demand("blue"))
    + job("producer")
    + job(
        "timed_box",
        "b",
        "date_conditions: 1\nstart_mins: 0,10,20\n"
        "condition: d(producer,00.02)\n" + demand("amber", "blue"),
    )
    + job("timed_child", extra="box_name: timed_box\n")
    + job("probe", extra=demand("amber"))
    + job("waiter", extra=demand("amber", "blue"))
)


def tick(runtime: Runtime, scheduler: Scheduler, minute: int) -> None:
    at = T0 + timedelta(minutes=minute)
    events = scheduler.pop_due(at)
    assert [(event.kind, event.at, event.payload) for event in events] == [
        ("STARTJOB", at, {"job": "timed_box"})
    ]
    for event in events:
        runtime.feed(event)


@pytest.mark.parametrize(
    "mode", ("0", "1", "2"), ids=("dsl41-default0", "vendor-default1", "mode2")
)
@pytest.mark.parametrize("release_at", (2.99, 3.0, 3.01), ids=("before", "inclusive", "expired"))
@pytest.mark.parametrize("outcome", ("SUCCESS", "FAILURE"))
def test_sem32_scheduled_done_atomic_vector(
    runtime_factory: Callable[..., Runtime], mode: str, release_at: float, outcome: str
) -> None:
    runtime = runtime_factory(SCHEDULED, {"queued-recheck": mode})
    scheduler = Scheduler(
        catalog(SCHEDULED),
        start=T0,
        default_tz="UTC",
        semantics=resolve_switches({"queued-recheck": mode}),
    )
    feed(runtime, 0, "STARTJOB", "amber_holder")
    feed(runtime, 0, "STARTJOB", "blue_holder")
    tick(runtime, scheduler, 0)
    assert runtime.store.job["timed_box"].armed
    assert_states(runtime, timed_box="INACTIVE", timed_child="INACTIVE")
    assert_runs(runtime, amber_holder=1, blue_holder=1)
    end(runtime, 1, "producer", outcome)
    assert_states(runtime, timed_box="QUE_WAIT", timed_child="INACTIVE")
    assert runtime.store.job["timed_box"].reservations == ()
    assert_runs(runtime, amber_holder=1, blue_holder=1)
    end(runtime, 2, "amber_holder")
    assert_states(runtime, timed_box="QUE_WAIT", timed_child="INACTIVE")
    assert_units(runtime, **{"r:blue": 1})
    feed(runtime, 2.1, "STARTJOB", "probe")
    assert_states(runtime, probe="RUNNING", timed_box="QUE_WAIT")
    assert_units(runtime, **{"r:amber": 1, "r:blue": 1})
    end(runtime, 2.2, "probe")
    assert_units(runtime, **{"r:blue": 1})
    end(runtime, release_at, "blue_holder")
    rejected = mode != "0" and release_at > 3
    if rejected:
        assert_states(runtime, timed_box="INACTIVE", timed_child="INACTIVE")
        assert not runtime.store.job["timed_box"].armed
        assert_units(runtime)
        assert_runs(runtime, amber_holder=1, blue_holder=1, probe=1)
        end(runtime, 9, "producer", outcome)
        assert_states(runtime, timed_box="INACTIVE", timed_child="INACTIVE")
        tick(runtime, scheduler, 10)
    assert_states(runtime, timed_box="RUNNING", timed_child="RUNNING")
    assert not runtime.store.job["timed_box"].armed
    assert_units(runtime, **{"r:amber": 1, "r:blue": 1})
    assert {r.bucket for r in runtime.store.job["timed_box"].reservations} == {"r:amber", "r:blue"}
    assert_runs(runtime, amber_holder=1, blue_holder=1, probe=1, timed_box=1, timed_child=1)
    finish = 11 if rejected else 4
    feed(runtime, finish, "STARTJOB", "waiter")
    assert_states(runtime, waiter="QUE_WAIT")
    end(runtime, finish + 0.1, "timed_child", outcome)
    assert_states(runtime, timed_box=outcome, timed_child=outcome, waiter="RUNNING")
    assert runtime.store.job["timed_box"].reservations == ()
    assert_units(runtime, **{"r:amber": 1, "r:blue": 1})
    end(runtime, finish + 0.2, "waiter")
    assert_units(runtime)
    next_tick = 20 if rejected else 10
    end(runtime, next_tick - 1, "producer", outcome)
    tick(runtime, scheduler, next_tick)
    assert_states(runtime, timed_box="RUNNING", timed_child="RUNNING")
    assert_units(runtime, **{"r:amber": 1, "r:blue": 1})
    assert_runs(
        runtime, amber_holder=1, blue_holder=1, probe=1, timed_box=2, timed_child=2, waiter=1
    )
    end(runtime, next_tick + 0.1, "timed_child", outcome)
    assert_states(runtime, timed_box=outcome)
    assert_units(runtime)


DEADLINE = (
    job("watch_box", "b")
    + job("watcher", "f", "box_name: watch_box\nterm_run_time: 2\nbox_terminator: y\nn_retrys: 0\n")
    + job("after_watch", extra="box_name: watch_box\ncondition: s(watcher)\n")
)


@pytest.mark.parametrize("switch", (None, "true", "false"), ids=("default", "true", "false"))
def test_sem14_fw_deadline_terminator(
    runtime_factory: Callable[..., Runtime], switch: str | None
) -> None:
    runtime = runtime_factory(
        DEADLINE, {} if switch is None else {"box-terminator-on-terminated": switch}
    )
    feed(runtime, 0, "STARTJOB", "watch_box")
    feed(runtime, 1.99, "STARTJOB", "after_watch")
    assert_states(runtime, watch_box="RUNNING", watcher="RUNNING", after_watch="INACTIVE")
    assert_runs(runtime, watch_box=1, watcher=1)
    feed(runtime, 2, "STARTJOB", "after_watch")
    assert_states(
        runtime,
        watch_box="RUNNING" if switch == "false" else "TERMINATED",
        watcher="TERMINATED",
        after_watch="INACTIVE",
    )
    assert_runs(runtime, watch_box=1, watcher=1)


def test_sem14_fw_completion_cancels_deadline(runtime_factory: Callable[..., Runtime]) -> None:
    runtime = runtime_factory(DEADLINE)
    feed(runtime, 0, "STARTJOB", "watch_box")
    end(runtime, 1, "watcher")
    assert_states(runtime, watch_box="RUNNING", watcher="SUCCESS", after_watch="RUNNING")
    end(runtime, 1.5, "after_watch")
    feed(runtime, 3, "STARTJOB", "after_watch")
    assert_states(runtime, watch_box="SUCCESS", watcher="SUCCESS", after_watch="SUCCESS")
    assert_runs(runtime, watch_box=1, watcher=1, after_watch=1)


OR_JOIN = (
    job("join_box", "b")
    + job("local_task", extra="box_name: join_box\n")
    + job("join_task", extra="box_name: join_box\ncondition: s(local_task) | s(outside_box)\n")
    + job("sentinel", extra="box_name: join_box\n")
    + job("outside_box", "b")
    + job("outside_task", extra="box_name: outside_box\n")
)


@pytest.mark.parametrize("first", ("local", "outside", "both", "neither"))
def test_sem03_or_join_cross_box_latch(runtime_factory: Callable[..., Runtime], first: str) -> None:
    runtime = runtime_factory(OR_JOIN)
    feed(runtime, 0, "STARTJOB", "outside_box")
    feed(runtime, 0, "STARTJOB", "join_box")
    assert_states(runtime, join_task="INACTIVE")
    assert_runs(runtime, outside_box=1, outside_task=1, join_box=1, local_task=1, sentinel=1)
    if first == "neither":
        feed(runtime, 1, "STARTJOB", "join_task")
        assert_states(runtime, join_task="INACTIVE")
        assert_runs(runtime, outside_box=1, outside_task=1, join_box=1, local_task=1, sentinel=1)
        return
    end(runtime, 1, "local_task" if first in ("local", "both") else "outside_task")
    if first == "both":
        end(runtime, 1, "outside_task")
    assert_states(runtime, join_task="RUNNING")
    end(runtime, 2, "join_task")
    if first == "local":
        end(runtime, 3, "outside_task")
    elif first == "outside":
        end(runtime, 3, "local_task")
    assert_states(runtime, join_box="RUNNING", join_task="SUCCESS", outside_box="SUCCESS")
    assert_runs(
        runtime, outside_box=1, outside_task=1, join_box=1, local_task=1, sentinel=1, join_task=1
    )
    end(runtime, 4, "sentinel")
    assert_states(runtime, join_box="SUCCESS")
    feed(runtime, 5, "STARTJOB", "join_box")
    assert_states(
        runtime,
        join_box="RUNNING",
        local_task="RUNNING",
        sentinel="RUNNING",
        join_task="RUNNING",
        outside_box="SUCCESS",
        outside_task="SUCCESS",
    )
    assert_runs(
        runtime, outside_box=1, outside_task=1, join_box=2, local_task=2, sentinel=2, join_task=2
    )


NESTED_DEADLINE = (
    resource("inner_unit")
    + resource("outer_unit")
    + job("outer_box", "b", demand("outer_unit"))
    + job("outer_sibling", extra="box_name: outer_box\n")
    + DEADLINE.replace(
        "insert_job: watch_box\njob_type: b\n",
        "insert_job: watch_box\njob_type: b\nbox_name: outer_box\n" + demand("inner_unit"),
    )
    + job("inner_waiter", extra=demand("inner_unit"))
    + job("outer_waiter", extra=demand("outer_unit"))
)


@pytest.mark.parametrize("switch", (None, "true", "false"), ids=("default", "true", "false"))
def test_sem14_nested_fw_deadline_resource_ownership(
    runtime_factory: Callable[..., Runtime], switch: str | None
) -> None:
    switches = {} if switch is None else {"box-terminator-on-terminated": switch}
    runtime = runtime_factory(NESTED_DEADLINE, switches)
    feed(runtime, 0, "STARTJOB", "outer_box")
    feed(runtime, 0.1, "STARTJOB", "inner_waiter")
    feed(runtime, 0.1, "STARTJOB", "outer_waiter")
    feed(runtime, 1.99, "STARTJOB", "after_watch")
    assert_states(
        runtime,
        outer_box="RUNNING",
        outer_sibling="RUNNING",
        watch_box="RUNNING",
        watcher="RUNNING",
        after_watch="INACTIVE",
        inner_waiter="QUE_WAIT",
        outer_waiter="QUE_WAIT",
    )
    assert_units(runtime, **{"r:inner_unit": 1, "r:outer_unit": 1})
    assert runtime.store.job["watcher"].reservations == ()
    assert_runs(runtime, outer_box=1, outer_sibling=1, watch_box=1, watcher=1)
    feed(runtime, 2, "STARTJOB", "after_watch")
    assert_states(
        runtime,
        watcher="TERMINATED",
        after_watch="INACTIVE",
        outer_box="RUNNING",
        outer_sibling="RUNNING",
        outer_waiter="QUE_WAIT",
    )
    assert [r.bucket for r in runtime.store.job["outer_box"].reservations] == ["r:outer_unit"]
    if switch == "false":
        assert_states(runtime, watch_box="RUNNING", inner_waiter="QUE_WAIT")
        assert [r.bucket for r in runtime.store.job["watch_box"].reservations] == ["r:inner_unit"]
        assert_runs(runtime, outer_box=1, outer_sibling=1, watch_box=1, watcher=1)
        return
    assert_states(runtime, watch_box="TERMINATED", inner_waiter="RUNNING")
    assert runtime.store.job["watch_box"].reservations == ()
    assert_runs(runtime, outer_box=1, outer_sibling=1, watch_box=1, watcher=1, inner_waiter=1)
    end(runtime, 2.1, "inner_waiter")
    assert_units(runtime, **{"r:outer_unit": 1})
    end(runtime, 3, "outer_sibling")
    assert_states(runtime, outer_box="FAILURE", outer_sibling="SUCCESS", outer_waiter="RUNNING")
    assert runtime.store.job["outer_box"].reservations == ()
    assert_units(runtime, **{"r:outer_unit": 1})
    assert_runs(
        runtime,
        outer_box=1,
        outer_sibling=1,
        watch_box=1,
        watcher=1,
        inner_waiter=1,
        outer_waiter=1,
    )
    end(runtime, 3.1, "outer_waiter")
    assert_units(runtime)


def test_sem14_nested_fw_success_releases_both_units(
    runtime_factory: Callable[..., Runtime],
) -> None:
    runtime = runtime_factory(NESTED_DEADLINE)
    feed(runtime, 0, "STARTJOB", "outer_box")
    end(runtime, 1, "watcher")
    assert_states(runtime, after_watch="RUNNING", watch_box="RUNNING", outer_box="RUNNING")
    end(runtime, 1.2, "after_watch")
    assert_states(runtime, watch_box="SUCCESS", outer_box="RUNNING")
    assert_units(runtime, **{"r:outer_unit": 1})
    end(runtime, 1.5, "outer_sibling")
    feed(runtime, 3, "STARTJOB", "after_watch")
    assert_states(
        runtime,
        watcher="SUCCESS",
        after_watch="SUCCESS",
        watch_box="SUCCESS",
        outer_sibling="SUCCESS",
        outer_box="SUCCESS",
        inner_waiter="INACTIVE",
        outer_waiter="INACTIVE",
    )
    assert_units(runtime)
    assert_runs(runtime, outer_box=1, outer_sibling=1, watch_box=1, watcher=1, after_watch=1)


def test_sem23_force_start_and_status_seed_kickoff(
    runtime_factory: Callable[..., Runtime],
) -> None:
    """SEM-23/04/10: a status write supplies history without executing its box."""
    source = resource("work_unit") + RING.replace(
        "insert_job: work_stage\njob_type: b\n",
        "insert_job: work_stage\njob_type: b\n" + demand("work_unit"),
    )
    runtime = runtime_factory(source)
    release(runtime, 0)
    assert all(not row.on_hold for row in runtime.store.job.values())
    assert_states(runtime, **dict.fromkeys(runtime.store.job, "INACTIVE"))
    assert_runs(runtime)
    assert_units(runtime)
    feed(runtime, 1, "FORCE_STARTJOB", "read_stage")
    assert_states(
        runtime,
        read_stage="RUNNING",
        read_marker="RUNNING",
        work_stage="INACTIVE",
        work_task="INACTIVE",
    )
    assert_runs(runtime, read_stage=1, read_marker=1)
    assert_units(runtime)
    end(runtime, 2, "work_stage")
    assert_states(
        runtime,
        read_stage="RUNNING",
        read_marker="RUNNING",
        work_stage="SUCCESS",
        work_task="INACTIVE",
    )
    assert runtime.store.job["work_stage"].last_end_at == T0 + timedelta(minutes=2)
    assert runtime.store.job["work_stage"].reservations == ()
    assert_runs(runtime, read_stage=1, read_marker=1)
    assert_units(runtime)
    for cycle in (1, 2):
        end(runtime, cycle * 4, "read_marker")
        assert_states(
            runtime,
            read_stage="SUCCESS",
            read_marker="SUCCESS",
            work_stage="RUNNING",
            work_task="RUNNING",
        )
        assert_runs(runtime, read_stage=cycle, read_marker=cycle, work_stage=cycle, work_task=cycle)
        assert [r.bucket for r in runtime.store.job["work_stage"].reservations] == ["r:work_unit"]
        assert_units(runtime, **{"r:work_unit": 1})
        end(runtime, cycle * 4 + 1, "work_task")
        assert_states(
            runtime,
            read_stage="RUNNING",
            read_marker="RUNNING",
            work_stage="SUCCESS",
            work_task="SUCCESS",
        )
        assert_runs(
            runtime, read_stage=cycle + 1, read_marker=cycle + 1, work_stage=cycle, work_task=cycle
        )
        assert runtime.store.job["work_stage"].reservations == ()
        assert_units(runtime)


@pytest.mark.parametrize("holcal", ("", "holcal:\n"), ids=("omitted", "blank"))
def test_sem36_parsed_holiday_p_requires_holcal(holcal: str) -> None:
    """SEM-36: parsed holiday P without its holiday reference is invalid input."""
    text = (
        "extended_calendar: month_marker\nnon_workday: S\nholiday: P\n"
        + holcal
        + "condition: FOM\n"
    )
    lowered = catalog(text)
    assert lowered.jobs == {}
    assert set(lowered.calendars) == {"month_marker"}
    calendar = lowered.calendars["month_marker"]
    assert calendar.kind == "extended"
    assert calendar.conditions == ["FOM"]
    assert calendar.attrs == (
        {"non_workday": "S", "holiday": "P", "holcal": ""}
        if holcal
        else {"non_workday": "S", "holiday": "P"}
    )
    with pytest.raises(CalendarRuleError, match="requires holcal"):
        compile_calendar(calendar, lowered)


def test_sem14_fw_failure_with_zero_retries(runtime_factory: Callable[..., Runtime]) -> None:
    """SEM-14: explicit FW failure ends its parent and schedules no retry."""
    runtime = runtime_factory(DEADLINE)
    feed(runtime, 0, "STARTJOB", "watch_box")
    end(runtime, 1, "watcher", "FAILURE")
    assert_states(runtime, watcher="FAILURE", watch_box="TERMINATED", after_watch="INACTIVE")
    assert_runs(runtime, watch_box=1, watcher=1)
    feed(runtime, 3, "STARTJOB", "after_watch")
    assert_states(runtime, watcher="FAILURE", watch_box="TERMINATED", after_watch="INACTIVE")
    assert_runs(runtime, watch_box=1, watcher=1)


def mutual_source(*, queued: bool) -> str:
    hold = "" if queued else "status: ON_HOLD\n"
    pool = (
        resource("copper_unit") + job("unit_holder", extra=demand("copper_unit")) if queued else ""
    )
    return (
        pool
        + job("copper_permission")
        + job("silver_permission")
        + job(
            "copper_box",
            "b",
            "condition: s(copper_permission) & n(silver_box)\n"
            + hold
            + (demand("copper_unit") if queued else ""),
        )
        + job("copper_task", extra="box_name: copper_box\n" + hold)
        + job("silver_box", "b", "condition: s(silver_permission,0) & n(copper_box)\n" + hold)
        + job("silver_task", extra="box_name: silver_box\n" + hold)
    )


@pytest.mark.parametrize("first", ("copper", "silver"), ids=("copper-first", "silver-first"))
def test_sem02_mutual_notrunning_ordinary_handoff(
    runtime_factory: Callable[..., Runtime], first: str
) -> None:
    """SEM-01/02/10/21: mutual status gates hand off in hold-release order."""
    runtime = runtime_factory(mutual_source(queued=False))
    second = "silver" if first == "copper" else "copper"
    assert all(
        runtime.store.job[name].on_hold
        for name in ("copper_box", "copper_task", "silver_box", "silver_task")
    )
    end(runtime, 0, "copper_permission")
    end(runtime, 0, "silver_permission")
    assert_runs(runtime)
    feed(runtime, 1, "OFF_HOLD", "copper_task")
    feed(runtime, 1.1, "OFF_HOLD", "silver_task")
    assert runtime.store.job["copper_box"].on_hold
    assert runtime.store.job["silver_box"].on_hold
    assert_states(
        runtime,
        copper_box="INACTIVE",
        copper_task="INACTIVE",
        silver_box="INACTIVE",
        silver_task="INACTIVE",
    )
    assert_runs(runtime)
    feed(runtime, 2, "OFF_HOLD", first + "_box")
    assert_states(
        runtime,
        **{
            first + "_box": "RUNNING",
            first + "_task": "RUNNING",
            second + "_box": "INACTIVE",
            second + "_task": "INACTIVE",
        },
    )
    assert_runs(runtime, **{first + "_box": 1, first + "_task": 1})
    feed(runtime, 3, "OFF_HOLD", second + "_box")
    assert all(not row.on_hold for row in runtime.store.job.values())
    assert_states(
        runtime,
        **{
            first + "_box": "RUNNING",
            first + "_task": "RUNNING",
            second + "_box": "INACTIVE",
            second + "_task": "INACTIVE",
        },
    )
    assert_runs(runtime, **{first + "_box": 1, first + "_task": 1})
    end(runtime, 4, first + "_task")
    assert_states(
        runtime,
        **{
            first + "_box": "SUCCESS",
            first + "_task": "SUCCESS",
            second + "_box": "RUNNING",
            second + "_task": "RUNNING",
        },
    )
    assert_runs(runtime, copper_box=1, copper_task=1, silver_box=1, silver_task=1)
    end(runtime, 5, second + "_task")
    if first == "copper":
        assert_states(
            runtime,
            copper_box="RUNNING",
            copper_task="RUNNING",
            silver_box="SUCCESS",
            silver_task="SUCCESS",
        )
        assert_runs(runtime, copper_box=2, copper_task=2, silver_box=1, silver_task=1)
        end(runtime, 6, "copper_task")
    copper_runs = 2 if first == "copper" else 1
    assert_states(
        runtime,
        copper_box="SUCCESS",
        copper_task="SUCCESS",
        silver_box="SUCCESS",
        silver_task="SUCCESS",
    )
    assert runtime.store.job["silver_permission"].last_end_at == T0
    assert runtime.store.job["silver_box"].last_end_at == T0 + timedelta(
        minutes=5 if first == "copper" else 4
    )
    assert_runs(
        runtime, copper_box=copper_runs, copper_task=copper_runs, silver_box=1, silver_task=1
    )
    end(runtime, 7, "silver_permission")
    assert_states(
        runtime,
        copper_box="SUCCESS",
        copper_task="SUCCESS",
        silver_box="RUNNING",
        silver_task="RUNNING",
    )
    assert_runs(
        runtime, copper_box=copper_runs, copper_task=copper_runs, silver_box=2, silver_task=2
    )
    end(runtime, 8, "silver_task")
    assert_states(
        runtime,
        copper_box="RUNNING",
        copper_task="RUNNING",
        silver_box="SUCCESS",
        silver_task="SUCCESS",
    )
    assert_runs(
        runtime,
        copper_box=copper_runs + 1,
        copper_task=copper_runs + 1,
        silver_box=2,
        silver_task=2,
    )
    assert_states(runtime, copper_permission="SUCCESS", silver_permission="SUCCESS")
    assert_units(runtime)


@pytest.mark.parametrize(
    "mode", ("0", "1", "2"), ids=("dsl41-default0", "vendor-default1", "mode2")
)
def test_sem02_mutual_notrunning_queue_readmission(
    runtime_factory: Callable[..., Runtime], mode: str
) -> None:
    """DL-50/DL-257: QUE_WAIT satisfies n(); mode 0 admits without rechecking it."""
    runtime = runtime_factory(mutual_source(queued=True), {"queued-recheck": mode})
    feed(runtime, 0, "STARTJOB", "unit_holder")
    end(runtime, 1, "copper_permission")
    assert_states(
        runtime,
        copper_box="QUE_WAIT",
        copper_task="INACTIVE",
        silver_box="INACTIVE",
        silver_task="INACTIVE",
        unit_holder="RUNNING",
    )
    assert runtime.store.job["copper_box"].reservations == ()
    assert [r.bucket for r in runtime.store.job["unit_holder"].reservations] == ["r:copper_unit"]
    assert_units(runtime, **{"r:copper_unit": 1})
    assert_runs(runtime, unit_holder=1)
    end(runtime, 2, "silver_permission")
    assert_states(
        runtime,
        copper_box="QUE_WAIT",
        copper_task="INACTIVE",
        silver_box="RUNNING",
        silver_task="RUNNING",
        unit_holder="RUNNING",
    )
    assert runtime.store.job["copper_box"].reservations == ()
    assert_units(runtime, **{"r:copper_unit": 1})
    assert_runs(runtime, unit_holder=1, silver_box=1, silver_task=1)
    end(runtime, 3, "unit_holder")
    assert runtime.store.job["unit_holder"].reservations == ()
    assert_states(runtime, silver_box="RUNNING", silver_task="RUNNING", unit_holder="SUCCESS")
    if mode == "0":
        assert_states(runtime, copper_box="RUNNING", copper_task="RUNNING")
        assert_runs(
            runtime, unit_holder=1, copper_box=1, copper_task=1, silver_box=1, silver_task=1
        )
        assert [r.bucket for r in runtime.store.job["copper_box"].reservations] == ["r:copper_unit"]
        assert_units(runtime, **{"r:copper_unit": 1})
    else:
        assert_states(runtime, copper_box="INACTIVE", copper_task="INACTIVE")
        assert runtime.store.job["copper_box"].reservations == ()
        assert_units(runtime)
        assert_runs(runtime, unit_holder=1, silver_box=1, silver_task=1)
    end(runtime, 4, "silver_task")
    assert_states(
        runtime,
        copper_box="RUNNING",
        copper_task="RUNNING",
        silver_box="SUCCESS",
        silver_task="SUCCESS",
        unit_holder="SUCCESS",
        copper_permission="SUCCESS",
        silver_permission="SUCCESS",
    )
    assert_runs(runtime, unit_holder=1, copper_box=1, copper_task=1, silver_box=1, silver_task=1)
    assert [r.bucket for r in runtime.store.job["copper_box"].reservations] == ["r:copper_unit"]
    assert_units(runtime, **{"r:copper_unit": 1})
