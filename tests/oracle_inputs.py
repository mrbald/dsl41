"""The oracle's inputs and renderings over the goldens' sample (DL-351).

The golden digests (tests/oracle_goldens.py) drive the oracle over these
inputs and digest these renderings after every input:

- `outcome`: whether the apply raised, and the exception's class and text;
- `trace`: the trace entries the input added, as the trace contract prints
  them (at, job, transition, cause);
- `emitted`: the events the input emitted (`InputBatch.emitted`);
- `store`: the store rows as canonical lines: jobs, globals, hosts, the
  timers in `(due, token)` order, `timer_seq`, `consumed` and
  `enqueue_counter`;
- `revisions`: the revisions the input moved, in the order the commit
  returned them (`InputBatch.revisions`);
- `pending_timers`: `Oracle.pending_timers()`, in its order;
- `next_timer_due`: the engine's wake-up read.

The store is written as the seal writes its rows: sets sorted, keys sorted,
reservations by bucket, rows by name. Equal stores render the same, so an
order the seal ignores never parts two runs.

At every `SealPoint` in a script, and once at the end, the seal digest is
the one over the seal's `state` section: the `SealedState` that
`Engine._snapshot` builds from the store, serialized as `Seal.to_payload`
serializes it. Its route table is left empty: the engine projects it from
its own executor id, and the oracle holds none. The other sections of a seal
are the engine's or derive from the rows and the pending timers, which are
digested on their own. `test_oracle_inputs` pins this serialization against
a real `close_runtime` seal.

The inputs: every lowerable corpus file and the whole corpus, with tier
(c)'s scripts and the estate generator's (`corpus_jobs`); the directed
estates (`directed_jobs`), each built for cells the random estates rarely
reach; the seeded estates (`generated_jobs`); and the switch matrix, every
other value of each semantic switch one at a time, and the
`SWITCH_PAIRS` (`switch_matrix`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from estate_gen import Advance, SealPoint, Step, catalog_script, seeded
from sealed_state import SealedHost, SealedState, _canon_ready

from dsl41.ast_jil import parse_file
from dsl41.canon import canonical_bytes, digest
from dsl41.cli_run import _trace_line
from dsl41.equiv import equiv_scripts
from dsl41.ir import CatalogIR, lower_catalog, lower_source
from dsl41.oracle import InputBatch, Oracle
from dsl41.oracle_state import Event
from dsl41.semantics import REGISTRY

CORPUS_DIR = Path(__file__).parent / "corpus"
#: the one corpus file that does not lower, on purpose (SEM-31)
CORPUS_UNLOWERABLE = frozenset({"sem31_xor.jil"})
#: the seal instant before any input has set one
EPOCH = datetime(2000, 1, 1)


# ------------------------------------------------------------------ renderings


def _json(value: object) -> str:
    return canonical_bytes(value).decode()


def describe(step: Step) -> str:
    """One line naming an input."""
    if isinstance(step, SealPoint):
        return "SEAL"
    if isinstance(step, Advance):
        return f"{step.at.isoformat()} ADVANCE"
    payload = " ".join(f"{key}={value}" for key, value in step.payload.items())
    source = f" source={step.source}" if step.source else ""
    return f"{step.at.isoformat()} {step.kind} {payload}{source}"


def _canonical(value: object) -> str:
    """`value` in the seal's canonical form: sets sorted (`_canon_ready`),
    object keys sorted (`canonical_bytes`)."""
    return _json(_canon_ready(value))


def _job_dump(row: Any) -> dict[str, Any]:
    """A job row as the seal writes it: reservations sorted by bucket."""
    dumped: dict[str, Any] = row.model_dump(by_alias=True)
    dumped["reservations"] = sorted(dumped["reservations"], key=lambda held: held["bucket"])
    return dumped


def _host_dump(row: Any) -> dict[str, Any]:
    dumped: dict[str, Any] = row.model_dump(by_alias=True)
    dumped["deadman_s"] = repr(row.deadman_s)  # the canonical grammar has no floats
    return dumped


class _Rows:
    """The store as canonical lines, written as the seal writes its rows:
    sets sorted, keys sorted, reservations by bucket, rows by name. So two
    equal stores render the same whatever order a set iterates in or a row
    was inserted in.

    A row's line is cached by the row's identity. Job, global and host rows
    are frozen all the way down (frozensets, tuples of frozen reservations),
    and every write replaces one, so only the rows an input replaced are
    serialized again; the cache holds each row it keys, so an identity is
    never reused while cached. Timer events are not frozen (an `Event`'s
    payload is a dict), so each is rendered afresh at every input."""

    def __init__(self) -> None:
        self._memo: dict[int, tuple[object, str]] = {}
        self._next: dict[int, tuple[object, str]] = {}

    def _row(self, prefix: str, name: str, row: Any, dump: Callable[[Any], object]) -> str:
        cached = self._memo.get(id(row))
        if cached is None or cached[0] is not row:
            cached = (row, f"{prefix}/{name} {_canonical(dump(row))}")
        self._next[id(row)] = cached
        return cached[1]

    def _rows(self, prefix: str, rows: Any, dump: Callable[[Any], object]) -> list[str]:
        return [self._row(prefix, name, rows[name], dump) for name in sorted(rows)]

    def render(self, oracle: Oracle) -> list[str]:
        store = oracle.store
        self._next = {}
        lines = self._rows("jobs", store.job, _job_dump)
        lines += self._rows("globals", store.globals_, lambda row: row.model_dump(by_alias=True))
        lines += self._rows("hosts", store.hosts, _host_dump)
        self._memo = self._next
        lines += [
            f"timer {_canonical([due, token, ev.model_dump(by_alias=True)])}"
            for due, token, ev in store.timers()
        ]
        lines += [f"consumed/{key} {units}" for key, units in sorted(store.consumed.items())]
        lines.append(f"timer_seq {store.timer_seq}")
        lines.append(f"enqueue_counter {store.enqueue_counter}")
        return lines


def seal_state_payload(state: SealedState) -> dict[str, Any]:
    """The `state` section exactly as `Seal.to_payload` writes it."""
    payload = state.model_dump(by_alias=True)
    payload["jobs"] = {
        name: {**row, "reservations": sorted(row["reservations"], key=lambda h: h["bucket"])}
        for name, row in payload["jobs"].items()
    }
    canon = _canon_ready(payload)
    assert isinstance(canon, dict)
    return canon


def sealed_state(oracle: Oracle, at: datetime) -> SealedState:
    """The `SealedState` `Engine._snapshot` builds from this store at `at`."""
    store = oracle.store
    return SealedState(
        jobs=dict(store.job),
        globals=dict(store.globals_),
        hosts={host_id: SealedHost.of(row) for host_id, row in store.hosts.items()},
        # the engine projects the route table from its own executor id, which
        # has a host row there and none here: not oracle state
        routes={},
        timers=tuple(store.timers()),
        timer_seq=store.timer_seq,
        consumed=dict(store.consumed),
        enqueue_counter=store.enqueue_counter,
        now=at,
    )


def seal_digest(oracle: Oracle, at: datetime) -> tuple[str, dict[str, Any] | None]:
    """The digest of the seal's `state` section, and the section. A state
    the seal model refuses gives the refusal and no section."""
    try:
        payload = seal_state_payload(sealed_state(oracle, at))
    except Exception as exc:  # noqa: BLE001 -- the refusal is the compared value
        return f"unsealable: {type(exc).__name__}: {exc}", None
    return digest({"state": payload}), payload


def seal_lines(sealed: tuple[str, dict[str, Any] | None]) -> list[str]:
    """The digest, then the section one row per line: what a seal
    divergence diffs."""
    found, payload = sealed
    lines = [f"digest {found}"]
    for key, value in (payload or {}).items():
        if isinstance(value, dict):
            lines += [f"{key}/{name} {_json(item)}" for name, item in value.items()]
        else:
            lines.append(f"{key} {_json(value)}")
    return lines


# ------------------------------------------------------------------ the pair


# ------------------------------------------------------------------ one oracle


def own(value: Any) -> Any:
    """A deep copy of a catalog or an input, for one run alone: the IR is
    mutable and read lazily, so an oracle that wrote to a shared one would
    reach a later run."""
    return value.model_copy(deep=True) if hasattr(value, "model_copy") else value


@dataclass
class _Applied:
    batch: InputBatch
    error: Exception | None
    observed: dict[str, list[str]] = field(default_factory=dict)


class Side:
    """One oracle, driven through `batch` and read through `trace_since`,
    `pending_timers`, `next_timer_due` and the store's rows, and what is kept
    about it between inputs."""

    def __init__(self, oracle: Oracle) -> None:
        self.oracle = oracle
        self.rows = _Rows()
        self.traced = 0

    def apply(self, step: Event | Advance) -> _Applied:
        at = step.at
        batch = self.oracle.batch(at)
        error: Exception | None = None
        try:
            with batch:
                if isinstance(step, Event):
                    batch.feed(step)
        except Exception as exc:  # noqa: BLE001 -- the outcome is what is digested
            error = exc
        return _Applied(batch, error)

    def restore(self, fork: Oracle, emitted: int) -> None:
        """Put this side's oracle back as `fork`, a fork taken before the
        input, in place, as the engine puts back an input that raised: the
        trace and the emitted log are cut back to their length then (a
        fork's start empty), and the store asks this oracle for its job
        instances again."""
        oracle = self.oracle
        trace, out = oracle._trace[: self.traced], oracle._emitted[:emitted]
        vars(oracle).update(vars(fork))
        oracle._trace, oracle._emitted = trace, out
        oracle.store.job_instance = oracle._job_instance

    def observe(self, batch: InputBatch | None, error: Exception | None) -> dict[str, list[str]]:
        oracle = self.oracle
        self.traced, entries = oracle.trace_since(self.traced)
        observed = {
            "outcome": [] if error is None else [f"{type(error).__name__}: {error}"],
            "trace": [_trace_line(entry) for entry in entries],
            "store": self.rows.render(oracle),
            "pending_timers": [
                f"{due.isoformat()} {job} {kind}" for due, job, kind in oracle.pending_timers()
            ],
            "next_timer_due": [str(oracle.next_timer_due())],
        }
        if batch is not None:
            observed["emitted"] = [_canonical(ev.model_dump(by_alias=True)) for ev in batch.emitted]
            observed["revisions"] = [f"{key} {rev}" for key, rev in batch.revisions.items()]
        return observed


def observations(oracle: Oracle, steps: Iterable[Event | Advance]) -> list[dict[str, list[str]]]:
    """What each input leaves on `oracle`, rendered as the goldens render it:
    an input that raises is observed with its outcome, and the next one goes
    on from what it left."""
    side = Side(oracle)
    found = []
    for step in steps:
        applied = side.apply(own(step))
        found.append(side.observe(applied.batch, applied.error))
    return found


# ------------------------------------------------------------------ inputs


#: Switches that reach the oracle only together with another: the oracle
#: compiles an extended calendar, so reads `wekr-first-week`, only in the
#: queued recheck, which `queued-recheck=0` turns off. Each pair is the
#: other value of the first switch under each value of the second that lets
#: it through; under the default the pair is the single-switch row.
SWITCH_PAIRS: tuple[tuple[str, str], ...] = (("wekr-first-week", "queued-recheck"),)


def switch_matrix() -> list[dict[str, str]]:
    """The defaults, then every other value of each switch, one at a time,
    then the `SWITCH_PAIRS`."""
    matrix: list[dict[str, str]] = [{}]
    for name, switch in REGISTRY.items():
        matrix += [{name: value} for value in switch.values if value != switch.default]
    for name, through in SWITCH_PAIRS:
        gate = REGISTRY[through]
        matrix += [
            {name: value, through: opened}
            for value in REGISTRY[name].values
            if value != REGISTRY[name].default
            for opened in gate.values
            if opened != gate.default
        ]
    return matrix


def switch_label(overrides: Mapping[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in overrides.items()) or "defaults"


@dataclass(frozen=True)
class Job:
    """One catalog and one script to run. `switches` fill the
    switches a matrix row leaves out, for a directed estate that needs a
    value other than the default to reach its cells; a row that sets one
    wins, so the job runs under every value the matrix names."""

    label: str
    catalog: CatalogIR
    script: tuple[Step, ...]
    switches: Mapping[str, str] = field(default_factory=dict)


def corpus_jobs(scripts: int = 3, length: int = 40) -> list[Job]:
    """Every lowerable corpus file, and the whole corpus as one catalog, each
    with `scripts` scripts from tier (c)'s generator and as many from the
    estate generator's (`estate_gen.catalog_script`)."""
    files = sorted(p for p in CORPUS_DIR.glob("*.jil") if p.name not in CORPUS_UNLOWERABLE)
    catalogs = [(p.name, lower_catalog([parse_file(p)])) for p in files]
    catalogs.append(("<whole corpus>", lower_catalog([parse_file(p) for p in files])))
    jobs: list[Job] = []
    for name, catalog in catalogs:
        if not catalog.jobs:
            continue
        for index, events in enumerate(equiv_scripts(catalog, scripts=scripts, seed=41)):
            jobs.append(Job(f"{name} tier-c script {index}", catalog, tuple(events)))
        for seed in range(scripts):
            script = catalog_script(catalog, seed, length)
            jobs.append(Job(f"{name} script seed {seed}", catalog, tuple(script)))
    return jobs


#: A directed estate for the `SWITCH_PAIRS` rows: random estates almost
#: never compile an extended calendar. 2014-01-01 is a Wednesday, so the
#: week-of-month rule `WEKRmon#02` holds 2014-01-06..12 under
#: `wekr-first-week=partial` and 2014-01-13..19 under `first-full`. Two
#: waiters queue on one-unit resources and their holders end on 2014-01-06:
#: `wk_skip` excludes the calendar's days (read under `queued-recheck` 1
#: and 2), `wk_only` runs on them (read under 2).
WEKR_JIL = """extended_calendar: wk_rule
condition: WEKRmon#02

insert_resource: WK_A
res_type: R
amount: 1

insert_resource: WK_B
res_type: R
amount: 1

insert_job: wk_hold_a
job_type: c
machine: m1
command: x
resources: (WK_A, QUANTITY=1, FREE=A)

insert_job: wk_hold_b
job_type: c
machine: m1
command: x
resources: (WK_B, QUANTITY=1, FREE=A)

insert_job: wk_skip
job_type: c
machine: m1
command: x
resources: (WK_A, QUANTITY=1)
date_conditions: 1
start_times: "08:00"
days_of_week: all
exclude_calendar: wk_rule

insert_job: wk_only
job_type: c
machine: m1
command: x
resources: (WK_B, QUANTITY=1)
date_conditions: 1
start_times: "08:00"
run_calendar: wk_rule
"""


def _wekr_script() -> tuple[Step, ...]:
    def event(at: datetime, kind: str, job: str, **payload: object) -> Event:
        return Event.model_validate({"at": at, "kind": kind, "payload": {"job": job, **payload}})

    day5, day6 = datetime(2014, 1, 5, 8, 0), datetime(2014, 1, 6, 8, 0)
    minute = timedelta(minutes=1)
    return (
        event(day5 - minute, "STARTJOB", "wk_hold_a"),
        event(day5, "STARTJOB", "wk_skip"),
        event(day6 - minute, "STARTJOB", "wk_hold_b"),
        event(day6, "STARTJOB", "wk_only"),
        event(day6 + minute, "STATUS", "wk_hold_a", status="SUCCESS"),
        event(day6 + 2 * minute, "STATUS", "wk_hold_b", status="SUCCESS"),
        SealPoint(),
    )


#: A directed estate for a box restarted inside its member's own box rules by
#: a STATUS that ends no run (DL-341): `rb` restarts on its own success,
#: and its member `rm` takes a unit when it runs. The seeded sample reaches
#: this rarely (seed 2870 alone of seeds 0 to 2999).
RESTART_JIL = """insert_resource: RR
res_type: R
amount: 1

insert_job: rb
job_type: b
condition: s(rb)

insert_job: rm
job_type: c
machine: m1
command: x
box_name: rb
resources: (RR, QUANTITY=1, FREE=Y)
"""


def _restart_scripts() -> list[tuple[Step, ...]]:
    def event(minutes: int, kind: str, job: str, **payload: object) -> Event:
        at = EPOCH + timedelta(minutes=minutes)
        return Event.model_validate({"at": at, "kind": kind, "payload": {"job": job, **payload}})

    never_run = (event(1, "STATUS", "rm", exit_code=0), SealPoint())
    after_a_failure = (
        event(1, "FORCE_STARTJOB", "rb"),
        event(2, "STATUS", "rm", status="FAILURE"),
        event(3, "STATUS", "rm", status="SUCCESS"),
        SealPoint(),
    )
    return [never_run, after_a_failure]


#: A directed estate for the settlement a restart owes first (DL-340,
#: DL-349): each box restarts on its own success and attempts its member
#: before the member's run settles, so the attempt takes job.401, the run
#: settles, and the attempt reads its admission after the settlement. A unit
#: freed on success admits the member again (`ow_free`), a kept one is taken
#: over (`ow_keep`), a spent one leaves nothing and the member queues
#: (`ow_spend`). The seeded sample reaches the shape only with renewables.
OWED_JIL = "".join(
    f"""insert_resource: OW_{name.upper()}
res_type: {kind}
amount: 1

insert_job: ob_{name}
job_type: b
condition: s(ob_{name})

insert_job: ow_{name}
job_type: c
machine: m1
command: x
box_name: ob_{name}
resources: (OW_{name.upper()}, QUANTITY=1{free})

"""
    for name, kind, free in (
        ("free", "R", ", FREE=Y"),
        ("keep", "R", ", FREE=N"),
        ("spend", "D", ""),
    )
)


def _owed_scripts() -> dict[str, tuple[Step, ...]]:
    def event(minutes: int, kind: str, job: str, **payload: object) -> Event:
        at = EPOCH + timedelta(minutes=minutes)
        return Event.model_validate({"at": at, "kind": kind, "payload": {"job": job, **payload}})

    return {
        name: (
            event(1, "FORCE_STARTJOB", f"ob_{name}"),
            event(2, "STATUS", f"ow_{name}", exit_code=0),
            SealPoint(),
            event(3, "STATUS", f"ow_{name}", exit_code=0),
            SealPoint(),
        )
        for name in ("free", "keep", "spend")
    }


#: A directed estate for the kill and timer cells the seeded sample does
#: not reach (DL-343). `kt_hold` holds the one unit.
#: - `kt_wait` is armed on hold, then queued by OFF_HOLD's retry: a kill on a
#:   waiter whose arm is latched (job.436).
#: - `kt_box` is a box that declares the unit (the generator gives boxes no
#:   resources): it queues behind `kt_hold` (a kill on a queued box, armed or
#:   not), or runs holding it with its terminator member `kt_bm` (a kill, a
#:   term_run_time, each settling the box's units).
#: - `kt_scan` queues behind `kt_hold` on 2000-01-01, which `kt_never`
#:   excludes with every day of the scan's two-year span, so the queued
#:   recheck sends it back to INACTIVE with a continuation at the span's end,
#:   2002-01-02 (DL-257): it resumes, or is refused because the job ran since
#:   or is no longer INACTIVE. `kt_mem` does the same inside box `kb`, so its
#:   continuation carries the box's run (DL-246) and can be box-stale.
#:   These need `queued-recheck` 1 or 2.
KILL_TIMER_JIL = (
    "calendar: kt_never\n"
    + "\n".join(f"{EPOCH.date() + timedelta(days=d):%m/%d/%Y}" for d in range(760))
    + """

insert_resource: KT_R
res_type: R
amount: 1

insert_job: kt_hold
job_type: c
machine: m1
command: x
resources: (KT_R, QUANTITY=1, FREE=Y)

insert_job: kt_wait
job_type: c
machine: m1
command: x
resources: (KT_R, QUANTITY=1, FREE=Y)
date_conditions: 1
days_of_week: all
start_times: "08:00"

insert_job: kt_box
job_type: b
resources: (KT_R, QUANTITY=1, FREE=Y)
term_run_time: 5
date_conditions: 1
days_of_week: all
start_times: "08:00"

insert_job: kt_bm
job_type: c
machine: m1
command: x
box_name: kt_box
job_terminator: 1

insert_job: kt_scan
job_type: c
machine: m1
command: x
resources: (KT_R, QUANTITY=1, FREE=Y)
date_conditions: 1
days_of_week: mo
exclude_calendar: kt_never
run_window: "07:00-10:00"

insert_job: kb
job_type: b

insert_job: kt_mem
job_type: c
machine: m1
command: x
box_name: kb
resources: (KT_R, QUANTITY=1, FREE=Y)
date_conditions: 1
days_of_week: mo
exclude_calendar: kt_never
run_window: "07:00-10:00"
"""
)
#: The scripts of `KILL_TIMER_JIL` that reach a scan continuation, and so
#: need `queued-recheck` 1 or 2.
KILL_TIMER_RESCANS = frozenset(
    {
        "rescan resumes",
        "rescan after a run",
        "rescan after a status",
        "rescan after a run, INACTIVE again",
        "member rescan, box restarted and member ran",
        "member rescan, box restarted",
    }
)


def _kill_timer_scripts() -> dict[str, tuple[Step, ...]]:
    def event(at: datetime, kind: str, job: str, **payload: object) -> Event:
        return Event.model_validate({"at": at, "kind": kind, "payload": {"job": job, **payload}})

    t0 = EPOCH + timedelta(hours=8)
    minute = timedelta(minutes=1)
    bound = datetime(2002, 1, 2)  # the continuation: midnight of the span's last day
    held = event(t0, "STARTJOB", "kt_hold")
    queued = (
        held,
        event(t0, "STARTJOB", "kt_scan"),
        event(t0 + minute, "STATUS", "kt_hold", status="SUCCESS"),
        SealPoint(),
    )
    member = (
        held,
        event(t0, "FORCE_STARTJOB", "kb"),
        event(t0, "STARTJOB", "kt_mem"),
        event(t0 + minute, "STATUS", "kt_hold", status="SUCCESS"),
        SealPoint(),
    )
    return {
        "armed waiter killed": (
            held,
            event(t0 + minute, "ON_HOLD", "kt_wait"),
            event(t0 + minute, "STARTJOB", "kt_wait"),
            event(t0 + 2 * minute, "OFF_HOLD", "kt_wait"),
            event(t0 + 3 * minute, "KILLJOB", "kt_wait"),
            SealPoint(),
        ),
        "queued box killed": (
            held,
            event(t0 + minute, "STARTJOB", "kt_box"),
            event(t0 + 2 * minute, "KILLJOB", "kt_box"),
            SealPoint(),
        ),
        "armed queued box killed": (
            held,
            event(t0 + minute, "ON_HOLD", "kt_box"),
            event(t0 + minute, "STARTJOB", "kt_box"),
            event(t0 + 2 * minute, "OFF_HOLD", "kt_box"),
            event(t0 + 3 * minute, "KILLJOB", "kt_box"),
            SealPoint(),
        ),
        "running box with units killed": (
            event(t0, "STARTJOB", "kt_box"),
            event(t0 + minute, "KILLJOB", "kt_box"),
            SealPoint(),
        ),
        "running box with units past term_run_time": (
            event(t0, "STARTJOB", "kt_box"),
            Advance(t0 + 6 * minute),
            SealPoint(),
        ),
        "rescan resumes": (
            *queued,
            Advance(bound + minute),
            Advance(datetime(2002, 2, 4, 7, 1)),
            SealPoint(),
        ),
        "rescan after a run": (
            *queued,
            event(t0 + 2 * minute, "FORCE_STARTJOB", "kt_scan"),
            event(t0 + 3 * minute, "STATUS", "kt_scan", status="SUCCESS"),
            Advance(bound + minute),
        ),
        "rescan after a status": (
            *queued,
            event(t0 + 2 * minute, "STATUS", "kt_scan", status="FAILURE"),
            Advance(bound + minute),
        ),
        "rescan after a run, INACTIVE again": (
            *queued,
            event(t0 + 2 * minute, "FORCE_STARTJOB", "kt_scan"),
            event(t0 + 3 * minute, "STATUS", "kt_scan", status="SUCCESS"),
            event(t0 + 4 * minute, "STATUS", "kt_scan", status="INACTIVE"),
            Advance(bound + minute),
        ),
        "member rescan, box restarted and member ran": (
            *member,
            event(t0 + 2 * minute, "STATUS", "kb", status="SUCCESS"),
            event(t0 + 3 * minute, "FORCE_STARTJOB", "kb"),
            event(t0 + 3 * minute, "FORCE_STARTJOB", "kt_mem"),
            event(t0 + 4 * minute, "STATUS", "kt_mem", status="SUCCESS"),
            Advance(bound + minute),
        ),
        "member rescan, box restarted": (
            *member,
            event(t0 + 2 * minute, "STATUS", "kb", status="SUCCESS"),
            event(t0 + 3 * minute, "STARTJOB", "kt_hold"),
            event(t0 + 3 * minute, "FORCE_STARTJOB", "kb"),
            Advance(bound + minute),
        ),
    }


#: A directed estate for the flag cells (DL-344) that the seeded sample
#: reaches rarely or never. `fl_hold` holds the one unit, so `fl_wait` and
#: the box `fl_box` queue behind it (the generator gives boxes no resources).
#: `fl_q`'s members can be busy while it is not running: `fl_qm` forced and
#: queued, `fl_qs` injected STARTING. `fl_run` runs with `fl_rn`; its member
#: `fl_rm` is iced first, and its subbox `fl_sub` waits on a job that never
#: runs, so it stays INACTIVE inside a RUNNING box, and its ice is the box's
#: completion moment (DL-285). `fl_dated` and `fl_box` arm on a held start
#: tick.
FLAG_JIL = """insert_resource: FL_R
res_type: R
amount: 1

insert_job: fl_hold
job_type: c
machine: m1
command: x
resources: (FL_R, QUANTITY=1, FREE=Y)

insert_job: fl_wait
job_type: c
machine: m1
command: x
resources: (FL_R, QUANTITY=1, FREE=Y)

insert_job: fl_job
job_type: c
machine: m1
command: x

insert_job: fl_dated
job_type: c
machine: m1
command: x
date_conditions: 1
days_of_week: all
start_times: "08:00"

insert_job: fl_box
job_type: b
resources: (FL_R, QUANTITY=1, FREE=Y)
date_conditions: 1
days_of_week: all
start_times: "08:00"

insert_job: fl_bm
job_type: c
machine: m1
command: x
box_name: fl_box

insert_job: fl_q
job_type: b

insert_job: fl_qm
job_type: c
machine: m1
command: x
box_name: fl_q
resources: (FL_R, QUANTITY=1, FREE=Y)

insert_job: fl_qs
job_type: c
machine: m1
command: x
box_name: fl_q

insert_job: fl_run
job_type: b

insert_job: fl_rm
job_type: c
machine: m1
command: x
box_name: fl_run

insert_job: fl_rn
job_type: c
machine: m1
command: x
box_name: fl_run

insert_job: fl_sub
job_type: b
box_name: fl_run
condition: s(fl_never)

insert_job: fl_sm
job_type: c
machine: m1
command: x
box_name: fl_sub

insert_job: fl_never
job_type: c
machine: m1
command: x
"""


def _flag_scripts() -> dict[str, tuple[Step, ...]]:
    def event(at: datetime, kind: str, job: str, **payload: object) -> Event:
        return Event.model_validate({"at": at, "kind": kind, "payload": {"job": job, **payload}})

    t0 = EPOCH + timedelta(hours=8)
    minute = timedelta(minutes=1)

    def at(n: int) -> datetime:
        return t0 + n * minute

    held = event(t0, "STARTJOB", "fl_hold")
    return {
        "flags on live jobs ignored": (
            event(t0, "STARTJOB", "fl_job"),
            event(at(1), "ON_ICE", "fl_job"),
            event(at(1), "ON_HOLD", "fl_job"),
            event(at(1), "ON_NOEXEC", "fl_job"),
            event(at(2), "STATUS", "fl_hold", status="STARTING"),
            event(at(3), "ON_ICE", "fl_hold"),
            event(at(3), "ON_HOLD", "fl_hold"),
            event(at(3), "ON_NOEXEC", "fl_hold"),
            SealPoint(),
        ),
        "ON_NOEXEC on an iced job ignored": (
            event(t0, "ON_ICE", "fl_job"),
            event(at(1), "ON_ICE", "fl_job"),
            event(at(1), "ON_NOEXEC", "fl_job"),
            event(at(2), "OFF_ICE", "fl_job"),
            event(at(2), "OFF_ICE", "fl_job"),
            SealPoint(),
        ),
        "ON_NOEXEC on boxes with busy members ignored": (
            held,
            event(at(1), "FORCE_STARTJOB", "fl_qm"),
            event(at(2), "ON_NOEXEC", "fl_q"),
            event(at(3), "STARTJOB", "fl_box"),
            event(at(4), "FORCE_STARTJOB", "fl_bm"),
            event(at(5), "ON_NOEXEC", "fl_box"),
            SealPoint(),
        ),
        "ON_NOEXEC on a box with a STARTING member ignored": (
            event(t0, "STATUS", "fl_qs", status="STARTING"),
            event(at(1), "ON_NOEXEC", "fl_q"),
            SealPoint(),
        ),
        "ON_HOLD and ON_ICE on waiters": (
            held,
            event(t0, "STARTJOB", "fl_wait"),
            event(at(1), "ON_HOLD", "fl_wait"),
            event(at(1), "ON_HOLD", "fl_wait"),
            event(at(2), "STATUS", "fl_hold", status="SUCCESS"),
            event(at(3), "OFF_HOLD", "fl_wait"),
            event(at(4), "STARTJOB", "fl_hold"),
            event(at(5), "ON_ICE", "fl_hold"),
            event(at(6), "STATUS", "fl_wait", status="SUCCESS"),
            SealPoint(),
        ),
        "ON_NOEXEC on waiters": (
            held,
            event(t0, "STARTJOB", "fl_wait"),
            event(at(1), "ON_HOLD", "fl_wait"),
            event(at(2), "ON_NOEXEC", "fl_wait"),
            event(at(3), "STARTJOB", "fl_job"),
            event(at(3), "OFF_NOEXEC", "fl_wait"),
            event(at(4), "STARTJOB", "fl_wait"),
            event(at(5), "ON_NOEXEC", "fl_wait"),
            SealPoint(),
        ),
        "ON_NOEXEC on a FAILED job": (
            event(t0, "STATUS", "fl_job", exit_code=1),
            event(at(1), "ON_NOEXEC", "fl_job"),
            event(at(2), "OFF_NOEXEC", "fl_job"),
            event(at(2), "OFF_NOEXEC", "fl_job"),
            SealPoint(),
        ),
        "flags on a queued box": (
            held,
            event(at(1), "STARTJOB", "fl_box"),
            event(at(2), "ON_HOLD", "fl_box"),
            event(at(3), "OFF_HOLD", "fl_box"),
            event(at(4), "DISARM", "fl_box"),
            event(at(5), "ON_ICE", "fl_box"),
            event(at(6), "OFF_NOEXEC", "fl_box"),
            SealPoint(),
        ),
        "ON_NOEXEC on a queued box": (
            held,
            event(at(1), "STARTJOB", "fl_box"),
            event(at(2), "ON_NOEXEC", "fl_box"),
            event(at(3), "STATUS", "fl_hold", status="SUCCESS"),
            SealPoint(),
        ),
        "ON_NOEXEC on a STARTING box": (
            event(t0, "STATUS", "fl_qs", status="INACTIVE", exit_code=3),
            event(at(1), "STATUS", "fl_q", status="STARTING"),
            event(at(2), "ON_NOEXEC", "fl_q"),
            event(at(3), "OFF_NOEXEC", "fl_q"),
            SealPoint(),
        ),
        "ON_NOEXEC on a STARTING box that holds a unit": (
            event(t0, "ON_HOLD", "fl_bm"),
            event(t0, "STARTJOB", "fl_box"),
            event(at(1), "STATUS", "fl_box", status="STARTING"),
            event(at(2), "STARTJOB", "fl_wait"),
            event(at(3), "ON_NOEXEC", "fl_box"),
            event(at(4), "OFF_HOLD", "fl_bm"),
            SealPoint(),
        ),
        "ON_NOEXEC on a held idle box": (
            event(t0, "ON_HOLD", "fl_q"),
            event(t0, "ON_HOLD", "fl_qs"),
            event(at(1), "ON_NOEXEC", "fl_q"),
            SealPoint(),
        ),
        "ON_NOEXEC on a box over a flagged and held job": (
            event(t0, "ON_NOEXEC", "fl_qs"),
            event(at(1), "ON_HOLD", "fl_qs"),
            event(at(2), "ON_NOEXEC", "fl_q"),
            SealPoint(),
        ),
        "DISARM on an armed job and box": (
            event(t0 - minute, "ON_HOLD", "fl_dated"),
            event(t0 - minute, "ON_HOLD", "fl_box"),
            event(t0, "STARTJOB", "fl_dated"),
            event(t0, "STARTJOB", "fl_box"),
            event(at(1), "DISARM", "fl_dated"),
            event(at(1), "DISARM", "fl_dated"),
            event(at(2), "DISARM", "fl_box"),
            event(at(3), "OFF_HOLD", "fl_box"),
            SealPoint(),
        ),
        "OFF_ICE in a running box, ON_NOEXEC on its idle subbox": (
            event(t0, "ON_ICE", "fl_rm"),
            event(at(1), "FORCE_STARTJOB", "fl_run"),
            event(at(2), "OFF_ICE", "fl_rm"),
            event(at(3), "ON_HOLD", "fl_sm"),
            event(at(4), "ON_NOEXEC", "fl_sub"),
            event(at(5), "OFF_NOEXEC", "fl_sub"),
            event(at(6), "STATUS", "fl_rn", status="SUCCESS"),
            SealPoint(),
        ),
        "ON_ICE on the last member a running box waits for": (
            event(t0, "FORCE_STARTJOB", "fl_run"),
            event(at(1), "STATUS", "fl_rm", status="SUCCESS"),
            event(at(1), "STATUS", "fl_rn", status="SUCCESS"),
            event(at(2), "ON_ICE", "fl_sub"),
            SealPoint(),
        ),
        "ON_NOEXEC on a job outside the catalog": (
            event(t0, "ON_HOLD", "fl_ghost"),
            event(at(1), "ON_NOEXEC", "fl_ghost"),
            SealPoint(),
        ),
    }


#: A directed estate for RELEASE_RESOURCE and the readmission (DL-345)
#: that the seeded sample reaches rarely or never: the generator gives boxes
#: no resources, keeps no units after a run (FREE=N), and names no machine
#: load. `qu_keep` keeps its unit of QU_K after a run, so `qu_w`, `qu_hi`,
#: `qu_lo` and `qu_arm` wait for its release; `qu_live` holds QU_R while it
#: runs, so `qu_take`, `qu_cond`, `qu_armc`, `qu_win` and the box member
#: `qu_bq` queue behind it. `qu_take` keeps a unit of QU_T, which it is
#: readmitted on. `qu_bx` holds QU_B and ends when `qu_bm` succeeds, with
#: `qu_bq` still queued; it waits for QU_B while `qu_bh` holds it. `qu_sb`
#: holds QU_B too, and its one member `qu_sm` queues behind `qu_live`; so do
#: `qu_tb` and its box_terminator member `qu_tm`, and `qu_p` (box_success:
#: s(qu_q)) with its box `qu_q`, which queues on QU_R while `qu_x` inside it
#: is forced to run; `qu_after` waits on s(qu_q). On
#: `qu_mach`, `qu_lm` (a member of `qu_lb`) blocks `qu_lx`, and `qu_two`
#: blocks `qu_nl`, which has no load (DL-247).
QUEUE_JIL = """insert_machine: qu_mach
type: a
node_name: qu_mach
max_load: 80

insert_resource: QU_K
res_type: R
amount: 1

insert_resource: QU_R
res_type: R
amount: 1

insert_resource: QU_T
res_type: R
amount: 1

insert_resource: QU_B
res_type: R
amount: 1

insert_job: qu_keep
job_type: c
machine: m1
command: x
resources: (QU_K, QUANTITY=1, FREE=N)

insert_job: qu_w
job_type: c
machine: m1
command: x
resources: (QU_K, QUANTITY=1, FREE=Y)

insert_job: qu_hi
job_type: c
machine: m1
command: x
priority: 1
resources: (QU_K, QUANTITY=1, FREE=Y)

insert_job: qu_lo
job_type: c
machine: m1
command: x
priority: 2
resources: (QU_K, QUANTITY=1, FREE=Y)

insert_job: qu_arm
job_type: c
machine: m1
command: x
date_conditions: 1
days_of_week: all
start_times: "08:00"
resources: (QU_K, QUANTITY=1, FREE=Y)

insert_job: qu_live
job_type: c
machine: m1
command: x
resources: (QU_R, QUANTITY=1, FREE=Y)

insert_job: qu_bare
job_type: c
machine: m1
command: x

insert_job: qu_take
job_type: c
machine: m1
command: x
resources: (QU_T, QUANTITY=1, FREE=N) AND (QU_R, QUANTITY=1, FREE=Y)

insert_job: qu_flag
job_type: c
machine: m1
command: x

insert_job: qu_cond
job_type: c
machine: m1
command: x
condition: s(qu_flag)
resources: (QU_R, QUANTITY=1, FREE=Y)

insert_job: qu_armc
job_type: c
machine: m1
command: x
date_conditions: 1
days_of_week: all
start_times: "08:00"
condition: s(qu_flag)
resources: (QU_R, QUANTITY=1, FREE=Y)

insert_job: qu_win
job_type: c
machine: m1
command: x
date_conditions: 1
days_of_week: all
run_window: "08:00-08:30"
resources: (QU_R, QUANTITY=1, FREE=Y)

insert_job: qu_bh
job_type: c
machine: m1
command: x
resources: (QU_B, QUANTITY=1, FREE=Y)

insert_job: qu_bx
job_type: b
box_success: s(qu_bm)
resources: (QU_B, QUANTITY=1, FREE=Y)

insert_job: qu_bm
job_type: c
machine: m1
command: x
box_name: qu_bx

insert_job: qu_bq
job_type: c
machine: m1
command: x
box_name: qu_bx
resources: (QU_R, QUANTITY=1, FREE=Y)

insert_job: qu_sb
job_type: b
resources: (QU_B, QUANTITY=1, FREE=Y)

insert_job: qu_sm
job_type: c
machine: m1
command: x
box_name: qu_sb
resources: (QU_R, QUANTITY=1, FREE=Y)

insert_job: qu_p
job_type: b
box_success: s(qu_q)
resources: (QU_B, QUANTITY=1, FREE=Y)

insert_job: qu_q
job_type: b
box_name: qu_p
resources: (QU_R, QUANTITY=1, FREE=Y)

insert_job: qu_x
job_type: c
machine: m1
command: x
box_name: qu_q

insert_job: qu_after
job_type: c
machine: m1
command: x
condition: s(qu_q)

insert_job: qu_tb
job_type: b
resources: (QU_B, QUANTITY=1, FREE=Y)

insert_job: qu_tm
job_type: c
machine: m1
command: x
box_name: qu_tb
box_terminator: 1
resources: (QU_R, QUANTITY=1, FREE=Y)

insert_job: qu_lh
job_type: c
machine: qu_mach
command: x
job_load: 30
priority: 1

insert_job: qu_lb
job_type: b

insert_job: qu_lm
job_type: c
machine: qu_mach
command: x
box_name: qu_lb
job_load: 60
priority: 5

insert_job: qu_lx
job_type: c
machine: qu_mach
command: x
job_load: 30
priority: 9

insert_job: qu_own
job_type: c
machine: qu_mach
command: x
job_load: 50
priority: 1

insert_job: qu_two
job_type: c
machine: qu_mach
command: x
job_load: 50
priority: 5

insert_job: qu_nl
job_type: c
machine: qu_mach
command: x
priority: 6
"""
#: The scripts of `QUEUE_JIL` whose waiter fails the queued recheck, and so
#: need `queued-recheck` 1 or 2.
QUEUE_RECHECKS = frozenset(
    {
        "a waiter whose condition is now false",
        "an armed waiter whose condition is now false",
        "a waiter whose window shut nearer its close",
        "a waiter whose window shut nearer its opening",
    }
)


def _queue_scripts() -> dict[str, tuple[Step, ...]]:
    def event(at: datetime, kind: str, job: str, **payload: object) -> Event:
        return Event.model_validate({"at": at, "kind": kind, "payload": {"job": job, **payload}})

    t0 = EPOCH + timedelta(hours=8)
    minute = timedelta(minutes=1)

    def at(n: int) -> datetime:
        return t0 + n * minute

    kept = (
        event(t0, "STARTJOB", "qu_keep"),
        event(t0, "STATUS", "qu_keep", status="SUCCESS"),
    )
    live = event(t0, "STARTJOB", "qu_live")
    return {
        "RELEASE_RESOURCE on live, idle and unknown jobs": (
            live,
            event(at(1), "RELEASE_RESOURCE", "qu_live"),
            event(at(1), "STARTJOB", "qu_bare"),
            event(at(2), "RELEASE_RESOURCE", "qu_bare"),
            event(at(2), "RELEASE_RESOURCE", "qu_keep"),
            event(at(3), "RELEASE_RESOURCE", "qu_ghost"),
            event(at(4), "STATUS", "qu_live", status="STARTING"),
            event(at(5), "RELEASE_RESOURCE", "qu_live"),
            SealPoint(),
        ),
        "a held job's release wakes its waiter": (
            *kept,
            event(at(1), "STARTJOB", "qu_w"),
            event(at(2), "RELEASE_RESOURCE", "qu_keep"),
            event(at(3), "RELEASE_RESOURCE", "qu_keep"),
            SealPoint(),
        ),
        "two waiters of different rank": (
            *kept,
            event(at(1), "STARTJOB", "qu_lo"),
            event(at(1), "STARTJOB", "qu_hi"),
            event(at(2), "RELEASE_RESOURCE", "qu_keep"),
            event(at(3), "STATUS", "qu_hi", status="SUCCESS"),
            SealPoint(),
        ),
        "a waiter readmitted on its held units": (
            event(t0, "STARTJOB", "qu_take"),
            event(t0, "STATUS", "qu_take", status="SUCCESS"),
            event(at(1), "STARTJOB", "qu_live"),
            event(at(1), "STARTJOB", "qu_take"),
            event(at(2), "STATUS", "qu_live", status="SUCCESS"),
            SealPoint(),
        ),
        "an armed waiter admitted": (
            *kept,
            event(t0, "ON_HOLD", "qu_arm"),
            event(t0, "STARTJOB", "qu_arm"),
            event(at(1), "OFF_HOLD", "qu_arm"),
            event(at(2), "RELEASE_RESOURCE", "qu_keep"),
            SealPoint(),
        ),
        "held, iced and noexec waiters": (
            *kept,
            event(at(1), "STARTJOB", "qu_w"),
            event(at(1), "ON_HOLD", "qu_w"),
            event(at(2), "RELEASE_RESOURCE", "qu_keep"),
            event(at(3), "OFF_HOLD", "qu_w"),
            event(at(4), "STARTJOB", "qu_hi"),
            event(at(4), "ON_ICE", "qu_hi"),
            event(at(5), "STARTJOB", "qu_lo"),
            event(at(5), "ON_NOEXEC", "qu_lo"),
            event(at(6), "STATUS", "qu_w", status="SUCCESS"),
            SealPoint(),
        ),
        "a waiter whose condition is now false": (
            live,
            event(at(1), "STATUS", "qu_flag", status="SUCCESS"),
            event(at(2), "STATUS", "qu_flag", status="FAILURE"),
            event(at(3), "STATUS", "qu_live", status="SUCCESS"),
            SealPoint(),
        ),
        "an armed waiter whose condition is now false": (
            live,
            event(t0, "STARTJOB", "qu_armc"),
            event(at(1), "STATUS", "qu_flag", status="SUCCESS"),
            event(at(2), "STATUS", "qu_flag", status="FAILURE"),
            event(at(3), "STATUS", "qu_live", status="SUCCESS"),
            SealPoint(),
        ),
        "a waiter whose window shut nearer its close": (
            live,
            event(at(1), "STARTJOB", "qu_win"),
            event(at(45), "STATUS", "qu_live", status="SUCCESS"),
            SealPoint(),
        ),
        "a waiter whose window shut nearer its opening": (
            live,
            event(at(1), "STARTJOB", "qu_win"),
            event(at(23 * 60 + 50), "STATUS", "qu_live", status="SUCCESS"),
            Advance(at(24 * 60 + 1)),
            SealPoint(),
        ),
        "a queued box is readmitted and starts its members": (
            event(t0, "STARTJOB", "qu_bh"),
            event(at(1), "STARTJOB", "qu_bx"),
            event(at(2), "STATUS", "qu_bh", status="SUCCESS"),
            event(at(3), "STATUS", "qu_bm", status="SUCCESS"),
            SealPoint(),
        ),
        "a box's release meets the member a STATUS took out of the queue": (
            live,
            event(at(1), "FORCE_STARTJOB", "qu_sb"),
            event(at(2), "STATUS", "qu_sm", status="INACTIVE"),
            SealPoint(),
        ),
        "a queued box's member ends it, and its parent's release meets it": (
            live,
            event(at(1), "FORCE_STARTJOB", "qu_p"),
            event(at(2), "FORCE_STARTJOB", "qu_x"),
            event(at(3), "STATUS", "qu_x", exit_code=0),
            SealPoint(),
        ),
        "a box's release meets its box_terminator member set FAILURE in the queue": (
            live,
            event(at(1), "FORCE_STARTJOB", "qu_tb"),
            event(at(2), "STATUS", "qu_tm", status="FAILURE"),
            SealPoint(),
        ),
        "a box's end cancels its queued member in its member's cascade": (
            live,
            event(at(1), "STARTJOB", "qu_bx"),
            event(at(2), "STATUS", "qu_bm", status="SUCCESS"),
            SealPoint(),
        ),
        "a stopped box's owed scan keeps its member and admits what it blocked": (
            event(t0, "STARTJOB", "qu_lh"),
            event(at(1), "STARTJOB", "qu_lb"),
            event(at(2), "STARTJOB", "qu_lx"),
            event(at(3), "KILLJOB", "qu_lb"),
            event(at(4), "STATUS", "qu_lh", status="SUCCESS"),
            SealPoint(),
        ),
        "a job with no load is admitted behind the waiter that blocked it": (
            event(t0, "STARTJOB", "qu_own"),
            event(at(1), "STARTJOB", "qu_two"),
            event(at(2), "STARTJOB", "qu_nl"),
            event(at(3), "STATUS", "qu_own", status="SUCCESS"),
            SealPoint(),
        ),
    }


#: A directed estate for the box rules (DL-346) that the corpus does not
#: reach. `bx_live` holds BX_R while it runs, so `bx_q` runs with its member
#: `bx_qm` queued behind it, and the boxes `bx_q1`, `bx_q2` and `bx_q3`,
#: which need BX_R too, queue: an idle queued box re-derives its status
#: when a member ends (box_success, box_failure, the default). `bx_sf` ends
#: SUCCESS, and its box_failure then holds. `bx_a` holds `bx_ah` on its
#: start (auto_hold); a scheduled tick arms it, and the box's kill drops
#: the arm. `bx_g`'s box_success names `bx_gc`, a member of its subbox
#: `bx_c`, which `bx_cw` keeps running: the override is the grandparent's.
BOX_JIL = """insert_resource: BX_R
res_type: R
amount: 1

insert_job: bx_live
job_type: c
machine: m1
command: x
resources: (BX_R, QUANTITY=1, FREE=Y)

insert_job: bx_never
job_type: c
machine: m1
command: x

insert_job: bx_q
job_type: b

insert_job: bx_qm
job_type: c
machine: m1
command: x
box_name: bx_q
resources: (BX_R, QUANTITY=1, FREE=Y)

insert_job: bx_q1
job_type: b
resources: (BX_R, QUANTITY=1, FREE=Y)
box_success: s(bx_q1m)

insert_job: bx_q1m
job_type: c
machine: m1
command: x
box_name: bx_q1

insert_job: bx_q2
job_type: b
resources: (BX_R, QUANTITY=1, FREE=Y)
box_failure: f(bx_q2m)

insert_job: bx_q2m
job_type: c
machine: m1
command: x
box_name: bx_q2

insert_job: bx_q3
job_type: b
resources: (BX_R, QUANTITY=1, FREE=Y)

insert_job: bx_q3m
job_type: c
machine: m1
command: x
box_name: bx_q3

insert_job: bx_sf
job_type: b
box_failure: f(bx_sfm)

insert_job: bx_sfm
job_type: c
machine: m1
command: x
box_name: bx_sf

insert_job: bx_sfn
job_type: c
machine: m1
command: x
box_name: bx_sf

insert_job: bx_a
job_type: b

insert_job: bx_ah
job_type: c
machine: m1
command: x
box_name: bx_a
auto_hold: 1
date_conditions: 1
days_of_week: all
start_times: "08:00"

insert_job: bx_g
job_type: b
box_success: s(bx_gc)

insert_job: bx_c
job_type: b
box_name: bx_g

insert_job: bx_gc
job_type: c
machine: m1
command: x
box_name: bx_c

insert_job: bx_cw
job_type: c
machine: m1
command: x
box_name: bx_c
condition: s(bx_never)

insert_job: bx_r
job_type: b
condition: d(bx_r)

insert_job: bx_rm
job_type: c
machine: m1
command: x
box_name: bx_r
box_terminator: 1
job_terminator: 1

insert_job: bx_rk
job_type: c
machine: m1
command: x
box_name: bx_r
job_terminator: 1
"""


def _box_scripts() -> dict[str, tuple[Step, ...]]:
    def event(at: datetime, kind: str, job: str, **payload: object) -> Event:
        return Event.model_validate({"at": at, "kind": kind, "payload": {"job": job, **payload}})

    t0 = EPOCH + timedelta(hours=8)
    minute = timedelta(minutes=1)

    def at(n: int) -> datetime:
        return t0 + n * minute

    live = event(t0, "STARTJOB", "bx_live")
    return {
        "a queued member goes INACTIVE with its box": (
            live,
            event(at(1), "FORCE_STARTJOB", "bx_q"),
            event(at(2), "STATUS", "bx_q", status="INACTIVE"),
            SealPoint(),
        ),
        "queued boxes re-derive their status as a member ends": (
            live,
            event(at(1), "STARTJOB", "bx_q1"),
            event(at(1), "STARTJOB", "bx_q2"),
            event(at(1), "STARTJOB", "bx_q3"),
            event(at(2), "STATUS", "bx_q1m", status="SUCCESS"),
            event(at(3), "STATUS", "bx_q2m", status="FAILURE"),
            event(at(4), "STATUS", "bx_q3m", status="FAILURE"),
            SealPoint(),
        ),
        "a box that ended SUCCESS meets its box_failure": (
            event(t0, "FORCE_STARTJOB", "bx_sf"),
            event(at(1), "STATUS", "bx_sfm", status="SUCCESS"),
            event(at(2), "STATUS", "bx_sfn", status="SUCCESS"),
            event(at(3), "STATUS", "bx_sfm", status="FAILURE"),
            SealPoint(),
        ),
        "an auto_hold member armed, and its box's kill drops the arm": (
            event(t0, "FORCE_STARTJOB", "bx_a"),
            event(t0, "STARTJOB", "bx_ah", source="scheduler"),
            event(at(1), "KILLJOB", "bx_a"),
            SealPoint(),
        ),
        "a grandparent's box_success names a member of its subbox": (
            event(t0, "FORCE_STARTJOB", "bx_g"),
            event(at(1), "STATUS", "bx_gc", status="SUCCESS"),
            SealPoint(),
        ),
        "a box that starts again on its own end meets its terminators": (
            event(t0, "FORCE_STARTJOB", "bx_r"),
            event(at(1), "STATUS", "bx_rm", status="FAILURE"),
            SealPoint(),
            event(at(2), "STATUS", "bx_rk", status="SUCCESS"),
            event(at(3), "STATUS", "bx_rm", status="FAILURE"),
            SealPoint(),
        ),
        "a box that starts again on its own end is killed": (
            event(t0, "FORCE_STARTJOB", "bx_r"),
            event(at(1), "KILLJOB", "bx_r"),
            SealPoint(),
            event(at(2), "KILLJOB", "bx_r"),
            SealPoint(),
        ),
    }


def directed_jobs() -> list[Job]:
    """The directed estates the matrix runs besides the corpus."""
    restart = lower_source(RESTART_JIL)
    kill_timer = lower_source(KILL_TIMER_JIL)
    flags = lower_source(FLAG_JIL)
    queue = lower_source(QUEUE_JIL)
    boxes = lower_source(BOX_JIL)
    owed = lower_source(OWED_JIL)
    return (
        [Job("<directed wekr>", lower_source(WEKR_JIL), _wekr_script())]
        + [
            Job(f"<directed idle restart {index}>", restart, script)
            for index, script in enumerate(_restart_scripts())
        ]
        + [
            Job(
                f"<directed kill and timer: {name}>",
                kill_timer,
                script,
                {"queued-recheck": "1"} if name in KILL_TIMER_RESCANS else {},
            )
            for name, script in _kill_timer_scripts().items()
        ]
        + [
            Job(f"<directed flags: {name}>", flags, script)
            for name, script in _flag_scripts().items()
        ]
        + [
            Job(
                f"<directed queue: {name}>",
                queue,
                script,
                {"queued-recheck": "1"} if name in QUEUE_RECHECKS else {},
            )
            for name, script in _queue_scripts().items()
        ]
        + [Job(f"<directed box: {name}>", boxes, script) for name, script in _box_scripts().items()]
        + [
            Job(f"<directed owed settlement: {name}>", owed, script)
            for name, script in _owed_scripts().items()
        ]
    )


def generated_jobs(seeds: Iterable[int], size: int, min_size: int = 1) -> list[Job]:
    """The seeded estates, each with `min_size` to `size` jobs."""
    jobs = []
    for seed in seeds:
        case = seeded(seed, size, min_size)
        jobs.append(Job(case.label, lower_source(case.jil), case.script))
    return jobs
