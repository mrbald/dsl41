"""Why a job has not started, read off the oracle for `explain` and `deps` (DL-534;
docs/control-protocol.md §4).

The answer keeps three questions apart:

- `wake`: what makes a start attempt for the job. Its next scheduled tick, or none scheduled; a
  run_window-deferred start; an update of a job or global its condition names; its box's
  start; for a waiter, a queue scan. An operator's start when nothing else applies.
- `disposition`: what the oracle does with one named attempt now. A job that is not live or
  queued gets the attempt a condition wake or a hold release makes (`start`), decided by
  `job_chart.start_verdict` itself. A waiter gets a full scan's readmission (`readmit`),
  decided by `readmit_verdict` for the waiter tried alone now: the scan tries the waiters ahead
  of it first, and what they take is not counted. `gates` lists the chart's tests in the
  chart's order (`START_GATES`, `READMIT_GATES`): the one that decides is `deciding`, each
  before it that applies to the job is `open`, and each after it that would also stop the
  attempt is `context`. A live job's disposition is `running`.
- `capacity`: the pool's own tests, bucket by bucket: each short bucket with its shortage, its
  spent units and the jobs that hold its units, and each waiter that blocks the job by priority.

What keeps a decided start from its dispatch is the engine's (`Engine.dispatch_wait`).

Every read is the oracle's. The payloads are the chart's own (`StartFacts`, `ReadmitFacts`),
whose facts are reads, so nothing is fed, popped or written and no row is created; only the
oracle's timezone and calendar caches fill, memos of name resolution. The pool's facts are
computed once (`capacity_view`) and seeded into the payload, so one answer visits each row a
bounded number of times; a waiter's answer also sorts the waiters. The instant is the
oracle's last input's (`as_of`). An oracle that has processed nothing has none, so `reading`
gives a fork stamped with the engine's present instant and the live oracle never advances. The
oracle package-privates read here (`_now`, `_pool`, `_counts_as_waiter`, `_cond_true`,
`_ancestor_boxes`) serve the oracle's truth, never a second implementation of it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from dsl41.capacity import DemandEntry, checks_load, without_machine_load
from dsl41.conditions import Cond, GlobalAtom, iter_atoms
from dsl41.derive import is_mutex_atom
from dsl41.ir import JobIR
from dsl41.job_chart import (
    READMIT_GATES,
    START_GATES,
    readmit_gate_closes,
    readmit_verdict,
    start_gate_closes,
    start_verdict,
)
from dsl41.job_chart_events import READMIT, START, JobData
from dsl41.job_chart_rows import row_configuration
from dsl41.oracle import Oracle
from dsl41.oracle_facts import ReadmitFacts, StartFacts, job_data
from dsl41.oracle_state import LIVE
from dsl41.state_machine import Context
from dsl41.state_machine import Event as ChartEvent

#: The start verdicts a disposition names otherwise: a member's skips are skips.
_START_DISPOSITIONS: Final = {"skip_member": "skip", "skip_inactive_member": "skip"}


def reading(oracle: Oracle, present: datetime) -> Oracle:
    """The oracle an answer reads: the live one, at its last input's instant, or, before its
    first input, a fork stamped at `present`, so the live oracle never advances (DL-534)."""
    if oracle._now is not None:
        return oracle
    twin = oracle.fork()
    twin._now = present
    return twin


def start_answer(oracle: Oracle, job: str) -> dict[str, Any]:
    """`as_of`, `attempt`, `disposition`, `gates`, and `capacity` where the job has a demand or
    the pool would refuse it, for catalog job `job` at the oracle's instant."""
    assert oracle._now is not None, "an answer reads an oracle with an instant (`reading`)"
    job_ir = oracle.catalog.jobs[job]
    row = oracle.store.job[job]
    answer: dict[str, Any] = {"as_of": oracle._now.isoformat()}
    if row.status in LIVE:
        return answer | {"attempt": None, "disposition": "running", "gates": []}
    active = row_configuration(row)
    data = job_data(oracle, job)
    key = oracle.store.job_key(job)
    if row.status == "QUE_WAIT":
        return answer | _readmission(oracle, job_ir, active, data, key)
    return answer | _start(oracle, job_ir, active, data, key)


def _state(deciding: str | None, closes: bool) -> str | None:
    """A gate's state given the gate that decided before it, if any: a later gate that would
    not stop the attempt is not reported."""
    if deciding is None:
        return "deciding" if closes else "open"
    return "context" if closes else None


def _gate(
    kind: str, state: str, text: str, job: str | None = None, at: datetime | None = None
) -> dict[str, Any]:
    return {
        "kind": kind,
        "state": state,
        "text": text,
        "job": job,
        "at": None if at is None else at.isoformat(),
    }


def _start(
    oracle: Oracle, job_ir: JobIR, active: frozenset[str], data: JobData, key: str
) -> dict[str, Any]:
    vector = oracle._pool.demand_vector(job_ir)
    view = capacity_view(oracle, job_ir, vector)
    # the pool's facts are read once (`capacity_view`), and the payload takes them
    known = {"vector": vector, "admissible": view["admissible"]}
    attempt = StartFacts(oracle, job_ir.name, job_ir, cause="explain", known=known)
    verdict = start_verdict(
        Context(key=key, event=ChartEvent(START, attempt), active=active, data=data)
    )
    gates: list[dict[str, Any]] = []
    deciding: str | None = None
    for gate in START_GATES:
        closes = start_gate_closes(gate, attempt, active, data.box)
        state = _state(deciding, closes)
        if state is None or (state == "open" and not _start_applies(gate, job_ir, data, attempt)):
            continue
        if state == "deciding":
            deciding = gate
        gates.append(_start_gate(oracle, gate, state, closes, attempt, job_ir))
    if deciding == "window" and verdict == "ignored":
        disposition = "defer" if attempt.window == "defer" else "skip"
    else:
        disposition = _START_DISPOSITIONS.get(verdict, verdict)
    answer: dict[str, Any] = {"attempt": START, "disposition": disposition, "gates": gates}
    if vector or not view["admissible"]:
        answer["capacity"] = view
    return answer


def _start_applies(gate: str, job_ir: JobIR, data: JobData, attempt: StartFacts) -> bool:
    """Whether an open gate is worth naming: the job has what it tests."""
    schedule = job_ir.schedule
    if gate == "date":
        return schedule is not None
    if gate == "box":
        return job_ir.box.box_name is not None
    if gate == "condition":
        return job_ir.sem.condition is not None
    if gate == "window":
        return schedule is not None and schedule.run_window is not None
    if gate == "noexec":
        return not data.box
    if gate == "capacity":
        return bool(attempt.vector())
    return True  # ice and hold: every job has the flags


def _start_gate(
    oracle: Oracle, gate: str, state: str, closes: bool, attempt: StartFacts, job_ir: JobIR
) -> dict[str, Any]:
    box = job_ir.box.box_name
    if gate == "ice":
        return _gate(gate, state, "on ice" if closes else "not on ice")
    if gate == "hold":
        return _gate(gate, state, "on hold" if closes else "not on hold")
    if gate == "date":
        text = _UNARMED if closes else "armed"
        return _gate(gate, state, text)
    if gate == "box":
        return _gate(gate, state, _BOX_TEXTS[attempt.box].format(box=box), job=box)
    if gate == "condition":
        return _gate(gate, state, "condition is false" if closes else "condition is true")
    if gate == "window":
        if not closes:
            return _gate(gate, state, "inside run_window")
        if attempt.window == "defer":
            text = "outside run_window: deferred to its next opening"
            return _gate(gate, state, text, at=attempt.known("next_open"))
        return _gate(gate, state, "outside run_window, nearer its last close: not run")
    if gate == "noexec":
        if not closes:
            return _gate(gate, state, "not on noexec")
        if "noexec_on" in row_configuration(oracle.store.job[job_ir.name]):
            return _gate(gate, state, "on noexec: completes without running")
        above = next(
            b for b in oracle._ancestor_boxes(job_ir.name) if oracle.store.job[b].on_noexec
        )
        return _gate(
            gate, state, f"box {above!r} is on noexec: completes without running", job=above
        )
    return _gate(gate, state, "short of capacity" if closes else "capacity is free")


#: The unarmed date gate (SEM-32): it stops every attempt but a STARTJOB's, which the scheduler
#: sends at a tick and an operator sends at will (`StartAttempt.scheduled`).
_UNARMED: Final = (
    "dated and not armed: a condition wake or a hold release stops here; a STARTJOB, a"
    " scheduled tick's or an operator's, passes"
)

#: The box gate's text by the member's gate on its box's run (`StartAttempt.box`).
_BOX_TEXTS: Final = {
    "idle": "box {box!r} is not running",
    "ran": "already ran in this run of box {box!r}",
    "iced_out": "taken off ice in this run of box {box!r}: sits the run out",
    "running": "box {box!r} is running",
}


#: A readmission's verdict as the disposition names it: what a scan does with the waiter tried
#: alone now. A `keep` is `held` or `short` by the gate that decides it.
_READMIT_DISPOSITIONS: Final = {"admit": "fits", "cancel": "cancel", "leave": "leave"}


def _readmission(
    oracle: Oracle, job_ir: JobIR, active: frozenset[str], data: JobData, key: str
) -> dict[str, Any]:
    vector = oracle._pool.demand_vector(job_ir)
    view = capacity_view(oracle, job_ir, vector, queued=True)
    known = {"vector": vector, "admissible": view["admissible"]}
    scan = ReadmitFacts(oracle, job_ir.name, job_ir, keep_stopped=False, known=known)
    verdict = readmit_verdict(
        Context(key=key, event=ChartEvent(READMIT, scan), active=active, data=data)
    )
    gates: list[dict[str, Any]] = []
    deciding: str | None = None
    for gate in READMIT_GATES:
        closes = readmit_gate_closes(gate, scan, active)
        state = _state(deciding, closes)
        if state is None or (state == "open" and not _readmit_applies(oracle, gate, job_ir)):
            continue
        if state == "deciding":
            deciding = gate
        gates.append(_readmit_gate(gate, state, closes, scan, job_ir))
    if verdict == "keep":
        disposition = "held" if deciding == "hold" else "short"
    else:
        disposition = _READMIT_DISPOSITIONS[verdict]
    return {"attempt": READMIT, "disposition": disposition, "gates": gates, "capacity": view}


def _readmit_applies(oracle: Oracle, gate: str, job_ir: JobIR) -> bool:
    """Whether an open gate is worth naming: the box gate for a member, the recheck under a
    `queued-recheck` that checks, the hold and capacity gates always."""
    if gate == "box":
        return job_ir.box.box_name is not None
    if gate == "recheck":
        return oracle.semantics.queued_recheck != "0"
    return True


def _readmit_gate(
    gate: str, state: str, closes: bool, scan: ReadmitFacts, job_ir: JobIR
) -> dict[str, Any]:
    box = job_ir.box.box_name
    if gate == "box":
        text = f"box {box!r} is not running" if closes else f"box {box!r} is running"
        return _gate(gate, state, text, job=box)
    if gate == "hold":
        return _gate(gate, state, "on hold" if closes else "not on hold")
    if gate == "capacity":
        return _gate(gate, state, "short of capacity alone now" if closes else "fits alone now")
    if not closes:
        return _gate(gate, state, "queued recheck passes")
    _check, why = scan.known("recheck")
    return _gate(gate, state, f"queued recheck fails: {why}")


#: What a waiter's capacity answer says of the scan.
_SCAN_TEXT: Final = (
    "a queue scan decides: the waiters ahead of it are tried first, and what they take is not"
    " counted here"
)


def capacity_view(
    oracle: Oracle, job_ir: JobIR, vector: list[DemandEntry], *, queued: bool = False
) -> dict[str, Any]:
    """The pool's admission test of an unforced start of `job_ir` alone now, part by part
    (DL-534): the higher-priority waiters that block it on a named resource, and on its
    machine's load when it checks load; then each bucket it tests that is short, with its spent
    units and the jobs that hold units of it. A short bucket may have no holder: its units are
    spent, or its threshold is below the demand. The job's own held units count as free to it
    (DL-256). Each part is one pass over the rows: the blockers' own load stage is read off
    the machines' floors (`CapacityPool.load_floors`), and a blocker that holds units gets the
    one usage total less its own reservations, so each row is visited a bounded number of
    times; `ahead` also sorts the waiters.

    For a waiter (`queued`), `ahead` lists the counted waiters the scan tries before it
    (`sorted_waiters`: priority, enqueue order, name) that request a bucket it needs, with
    those buckets, and `text` says that a scan decides."""
    pool = oracle._pool
    rows, consumed = oracle.store.job, oracle.store.consumed
    counts = oracle._counts_as_waiter
    floors = pool.load_floors(rows, consumed, counts)
    blockers = [
        {"job": waiter, "kind": "resource"}
        for waiter in pool.resource_blockers(job_ir, rows, consumed, counts, floors=floors)
    ]
    tested = vector
    if checks_load(job_ir):
        blockers += [
            {"job": waiter, "kind": "load"}
            for waiter in pool.load_blockers(job_ir, rows, consumed, counts)
        ]
    else:
        tested = without_machine_load(vector)
    short = pool.shortages(tested, rows, consumed, own=job_ir.name)
    held = pool.holders([bucket for bucket, *_ in short], rows, own=job_ir.name) if short else {}
    view: dict[str, Any] = {
        "admissible": not blockers and not short,
        "blockers": blockers,
        "short": [
            {
                "bucket": bucket,
                "capacity": capacity,
                "demand": demand,
                "used": used,
                "short": used + demand - capacity,
                "spent": consumed.get(bucket, 0),
                "holders": [{"job": name, "units": units} for name, units in held[bucket]],
            }
            for bucket, capacity, demand, used in short
        ],
    }
    if queued:
        view["ahead"] = _ahead(oracle, job_ir, vector)
        view["text"] = _SCAN_TEXT
    return view


def _ahead(oracle: Oracle, job_ir: JobIR, vector: list[DemandEntry]) -> list[dict[str, Any]]:
    """The counted waiters a scan tries before `job_ir` that request a bucket it needs."""
    pool = oracle._pool
    needs = {entry[0] for entry in vector}
    ahead: list[dict[str, Any]] = []
    waiters = pool.sorted_waiters(oracle.store.job)
    # a QUE_WAIT row always carries its rank (DL-120), so the waiter is in the list
    for waiter in waiters[: waiters.index(job_ir.name)]:
        shared = needs & {entry[0] for entry in pool.demand_vector(oracle.catalog.jobs[waiter])}
        if shared and oracle._counts_as_waiter(waiter):
            ahead.append({"job": waiter, "buckets": sorted(shared)})
    return ahead


def wake(oracle: Oracle, job: str, tick: datetime | None) -> list[dict[str, Any]]:
    """What makes a start attempt for catalog job `job` (DL-534). `tick` is its next scheduled
    tick, as the scheduler indexes it: a tick the scheduler still owes is named whatever the
    applied definition says, since it fires. A waiter is tried by a queue scan alone. A dated
    job with no tick says so: its condition wakes meet its date gate. A condition wakes the job
    whenever
    a job or global it names is updated and the condition is true, the same value again
    included, not only when it turns true. An operator's start is named when nothing else
    would make an attempt."""
    job_ir = oracle.catalog.jobs[job]
    if oracle.store.job[job].status == "QUE_WAIT":
        return [_wake("scan", "a queue scan: units are released or a block lifts")]
    found: list[dict[str, Any]] = []
    if tick is not None:
        found.append(_wake("tick", "its next scheduled tick", at=tick))
    elif job_ir.schedule is not None:
        found.append(_wake("tick", "none scheduled"))
    found += [
        _wake("deferred", "its run_window-deferred start", at=due)
        for due, name, kind in oracle.pending_timers()
        if name == job and kind == "run_window"
    ]
    if job_ir.sem.condition is not None:
        text = "a job or global the condition names is updated while the condition is true"
        found.append(_wake("condition", text))
    box = job_ir.box.box_name
    if box is not None:
        found.append(_wake("box", f"box {box!r} starts", job=box))
    if all(entry["kind"] == "tick" and entry["at"] is None for entry in found):
        found.append(_wake("operator", "only an operator's start"))
    return found


def _wake(
    kind: str, text: str, *, job: str | None = None, at: datetime | None = None
) -> dict[str, Any]:
    return {"kind": kind, "text": text, "job": job, "at": None if at is None else at.isoformat()}


def atom_entries(oracle: Oracle, job: str, cond: Cond, *, truth: bool) -> list[dict[str, Any]]:
    """Each atom of `job`'s condition in source order with its identity (DL-534): `job`, the
    key it names (null for a global); `kind`; `lookback`, its token; `guard`, a local
    unqualified n() (`derive.is_mutex_atom`); `self`; `undefined`, a local name the catalog
    does not define; `external`, another instance's job. With `truth`, the oracle's own truth
    (`true`: ice bypass, lookback, instances) and, for a global, its effective value (`actual`,
    null when never set, DL-66), since truth alone hides why; `global` and `globals` answer a
    named global (DL-87), and this reads the same value where the condition uses it. The
    condition's truth is the oracle's too, so a false atom inside a satisfied OR is no
    blocker."""
    from dsl41.dsl import cond_to_source  # heavyweight surface: load on demand

    entries: list[dict[str, Any]] = []
    for atom in iter_atoms(cond):
        entry: dict[str, Any] = {"atom": cond_to_source(atom), "kind": atom.kind}
        if isinstance(atom, GlobalAtom):
            entry |= {"job": None, "lookback": None, "self": False}
            entry |= {"undefined": False, "external": False}
        else:
            local = atom.job.instance is None
            entry |= {
                "job": atom.job.key,
                "lookback": None if atom.lookback is None else atom.lookback.raw,
                "self": local and atom.job.name == job,
                "undefined": local and atom.job.name not in oracle.catalog.jobs,
                "external": not local,
            }
        entry["guard"] = is_mutex_atom("condition", atom)
        if truth:
            entry["true"] = oracle._cond_true(atom, job)
            if isinstance(atom, GlobalAtom):
                entry["actual"] = oracle.store.global_value(atom.name)
        entries.append(entry)
    return entries
