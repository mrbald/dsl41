"""Taking possession of a run root: genesis and restart (DL-74, DL-106, DL-424, DL-433).

The Engine is the single-writer loop over a live estate. What lives here runs once per
incarnation, before that loop takes its first step: it creates or opens the run root's store
under its lock, restores and replays it, and hands back an Engine whose loop catches up with what
the last incarnation left open. The name avoids "lifecycle", which DL-42 gives to the wrapper
tier.

A run root holds `engine.db` (the store), `leader.lock`, `engine.toml`, `runs/` (the wrapper
spools) and `plans/`.

A restart is the live engine starting with a non-empty state (spec-restart). The engine is always
up, an engine death ends every running attempt, and at restart the engine owns nothing alive. No
step of the restart waits on a process, a wrapper or a record:

1. refuse before the first write: `flock leader.lock` with the holder named; the store's
   `state_version`, its `root_id` and its clock domain; a launch estate
   other than the applied configuration or the open update's candidate, job by job; an open
   update's missing plan directory;
2. fold: the snapshot and the tail into the oracle, the frontiers, the decisions, the evidence,
   the open table, the update in STOPPING with its filter (`EngineUpdate.resume`); every open
   attempt instantiated in the attempt tier, in SPAWNING, or in ENDED with its committed outcome
   when it is closing; file watches taken up from `watch.jsonl` (DL-242);
3. the first reading: one cookie scan of the run root, where the cookie can be read; what it
   finds of an attempt whose stop is owed is TERMed, the first rung of the ladder the loop runs;
4. the catch-up, as steps of the loop: one observation of each attempt without an outcome
   (`spawn_recorded`, its recorded stop, then its record or `engine_died`) and one cleanup of
   each closing one, in seq order; then the owed plans in seq order and the pending starts; then
   the time jump, after which the barrier lifts: the deferred dispatches go and `engine started`
   is recorded with the scheduler ticks missed while down (E9).

An attempt whose records do not read, or whose processes cannot be read, is quarantined: one
`unobservable` outcome with cause `quarantined`, its job held with the evidence, the engine up.
The plans follow the interruption rule (spec-wiring 2b): a natural end or a failed launch
completes; a stopped or unobservable attempt is dropped when its job left the catalog or the
oracle ended its run, held when its job is named in `non-idempotent.toml`, and otherwise run
again as a new attempt of the same run with the same invocation. A recovered plan step is
stamped at the end its outcome holds, never before the admission frontier, and fed before any
outage deadline. The engine lifecycle leaves STARTING at the barrier (`replay_done`). A restart
from a snapshot opens its trace with a `RESTART` line: the trace covers what follows the
snapshot.

`start_run` is genesis: an empty store has nothing to replay or catch up with.
"""

from __future__ import annotations

import contextlib
import os
import signal
import uuid

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, cast

from dsl41 import runner_procid as _procid
from dsl41.engine_charts import ENGINE_STOP, AttemptOutcome, ExitRecord
from dsl41.ir import CatalogIR, JobIR
from dsl41.reload_diff import catalog_hash_v2
from dsl41.oracle import Oracle
from dsl41.oracle_state import LIVE, Event
from dsl41.runner import (
    Deferred,
    Engine,
    adapter_switch,
    refit_adapters,
)
from dsl41.runner_adapters import (
    JobAdapter,
    load_json,
    read_watch_log,
    spool_names_run,
)
from dsl41.runner_attempts import NO_GROUP, command_group, exit_record_from_status
from dsl41.runner_clock import Clock, EngineError
from dsl41.runner_config import (
    Configuration,
    EngineSettings,
    RuntimeProfile,
    oracle_reading,
    profile_drift,
    switches_of,
    to_us,
    tz_aliases_of,
    with_host_settings,
)
from dsl41.runner_journal import (
    STORE_NAME,
    best_effort_report,
    Journal,
    OpenAttempt,
    Position,
    ReplayFault,
    decode_snapshot,
    dispatched_runs,
    first_rev,
    load_configuration,
    pending_record,
    replay_inputs,
    restore_oracle,
    wire_record,
)
from dsl41.runner_ledger import STATE_MACHINE_VERSION, LeaderLock, acquire_run_root
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_stops import AttemptFacts
from dsl41.runner_store import Record, SqliteStore, Store, StoreError
from dsl41.semantics import ADAPTER_SWITCHES, SCHEDULER_SWITCHES, check_overrides
from dsl41.state_machine import Executor


def _derive_runtime_profile(
    scheduler: Scheduler | None,
    adapters: Mapping[str, JobAdapter],
    base: RuntimeProfile | None,
    declared: RuntimeProfile | None = None,
) -> RuntimeProfile:
    """The runtime profile the engine is actually wired with (period-model ss2.1, DL-130).

    The profile describes the machine that runs, not the flags somebody typed: an embedder that
    builds a Europe/Zurich scheduler would otherwise record UTC while every tick is shifted. So
    the wired components are read back (the scheduler's timezone and switches, the adapters'
    windows and switches) over `base` for the fields the engine cannot see. `declared` is what
    the launcher was invoked with; it supplies the machine identity and policy, which no wired
    object reports (DL-151)."""
    from dsl41.runner_adapters import FileWatcherAdapter

    values: dict[str, object] = dict((base or RuntimeProfile()).model_dump())
    if declared is not None:
        values["as_machine"] = declared.as_machine
        values["machine_policy"] = declared.machine_policy
    if scheduler is not None:
        values["default_tz"] = scheduler.default_tz or "UTC"
        values["tz_aliases"] = dict(scheduler.tz_aliases)
    switches = dict((declared or base or RuntimeProfile()).semantics)
    if scheduler is not None:
        for name in SCHEDULER_SWITCHES:
            switches[name] = scheduler.semantics.value(name)
    fw = adapters.get("FW")
    if isinstance(fw, FileWatcherAdapter):
        values["fw_default_interval_us"] = to_us(float(fw.default_interval_s))
    # the adapter decides FW completeness, not the profile (DL-258): a wired adapter's switches
    # are read back like the scheduler's
    for name in ADAPTER_SWITCHES:
        wired = adapter_switch(adapters, name)
        if wired is not None:
            switches[name] = wired
    values["semantics"] = check_overrides(switches)
    return RuntimeProfile.model_validate(values)


@dataclass
class Wiring:
    """The components a `RuntimeProfile` and the engine settings say an incarnation runs
    with."""

    adapters: dict[str, JobAdapter]
    scheduler: Scheduler
    #: the process's statechart executor (statechart-engine ss11); the caller hands the same one
    #: to `start_run` or `resume_run`, so a process with an engine has one executor
    machines: Executor = field(default_factory=Executor)


def wire_from_profile(
    catalog: CatalogIR,
    profile: RuntimeProfile,
    *,
    start: datetime,
    settings: EngineSettings | None = None,
) -> Wiring:
    """Build the adapters and the scheduler from `profile` and `settings`, with the scheduler
    started at `start`. This function reads no clock (DL-145). The one place a real run's
    components are constructed (DL-137)."""
    from dsl41.runner_adapters import FileWatcherAdapter, LocalCommandAdapter

    settings = settings or EngineSettings()
    adapters = {
        "CMD": LocalCommandAdapter(grace_seconds=settings.grace_term_s),
        "FW": FileWatcherAdapter(
            default_interval_s=max(1, round(profile.fw_default_interval_us / 1_000_000)),
            existence=switches_of(profile).fw_existence,
        ),
    }
    scheduler = Scheduler(
        catalog,
        start=start,
        default_tz=profile.default_tz,
        # the profile cannot spell "no map": an absent `--timezone-map` is an empty table on it,
        # and passing that dict on would drop SEM-35's unique-city default for every per-job
        # zone. `tz_aliases_of` owns that question (DL-163, DL-151)
        tz_aliases=tz_aliases_of(profile),
        semantics=switches_of(profile),
    )
    return Wiring(adapters=cast("dict[str, JobAdapter]", adapters), scheduler=scheduler)


def _require_scheduler(catalog: CatalogIR, scheduler: Scheduler | None, where: str) -> None:
    """A catalog that schedules jobs needs a scheduler wired, and the right one (DL-130):
    without one scheduled execution stops, and one built over another catalog fires another
    estate's plans."""
    if scheduler is not None:
        if scheduler.catalog_hash != catalog_hash_v2(catalog):
            raise EngineError(
                f"the {where}'s scheduler was built over a different catalog --"
                " its plans are not this estate's (period-model ss2.1)"
            )
        return
    scheduled = sorted(
        name
        for name, job in catalog.jobs.items()
        if job.schedule is not None
        and (
            job.schedule.start_times
            or job.schedule.start_mins
            or job.schedule.run_calendar is not None
        )
    )
    if scheduled:
        raise EngineError(
            f"this catalog schedules {len(scheduled)} job(s) ({scheduled[0]}, ...)"
            f" and the {where} wired no scheduler -- scheduled execution would"
            " silently stop (period-model ss2.1)"
        )


def _require_adapters(catalog: CatalogIR, adapters: Mapping[str, JobAdapter], where: str) -> None:
    """Every executable job type must have an adapter wired (DL-130), or a job of that type
    reaches RUNNING with no process behind it."""
    required = sorted(
        {job.job_type for job in catalog.jobs.values() if job.job_type != "BOX"} - set(adapters)
    )
    if required:
        raise EngineError(
            f"this catalog runs job type(s) {', '.join(required)} and the {where}"
            " wired no adapter for them -- their jobs would run with no process"
            " behind them (period-model ss2.1)"
        )


def _clock_domain(clock: Clock) -> str:
    return "virtual" if clock.virtual else "real"


def refuse_old_root(run_root: Path) -> None:
    """A 1.x run root is refused by name: 2.0.0 reads `engine.db` only."""
    if (run_root / "journal.jsonl").exists() or (run_root / "wal").is_dir():
        raise EngineError(
            f"{run_root}: a 1.x run root (journal.jsonl or wal/): 2.0.0 runs from"
            f" {STORE_NAME} only, and a 1.x root cannot be resumed; pick a fresh run root"
        )


def start_run(
    catalog: CatalogIR,
    run_root: Path | None,
    *,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    scheduler: Scheduler | None = None,
    hold_open: bool = False,
    lock: LeaderLock | None = None,
    launched: RuntimeProfile | None = None,
    non_idempotent: Mapping[str, str] | None = None,
    machines: Executor | None = None,
    settings: EngineSettings | None = None,
    source_capture: Mapping[str, str] | None = None,
) -> Engine:
    """Genesis: a new store with configuration rev 1, and an Engine over it.

    With a run root: take its lock, refuse a used root, create `runs/`, `plans/` and
    `engine.db`, write the configuration and `engine started`. Without one (a rehearsal), the
    same records go to a `MemoryStore`. `launched` is the launch's profile; the wired
    components are read back over it. A failure releases the lock."""
    if run_root is None:
        return _genesis(
            catalog,
            None,
            None,
            clock=clock,
            adapters=adapters,
            scheduler=scheduler,
            hold_open=hold_open,
            launched=launched,
            non_idempotent=non_idempotent,
            machines=machines,
            settings=settings,
            source_capture=source_capture,
        )
    lock = lock or acquire_run_root(run_root)
    try:
        refuse_old_root(run_root)
        if (run_root / STORE_NAME).exists():
            raise EngineError(
                f"{run_root / STORE_NAME} already exists: a new run needs a fresh run root, and"
                " `dsl41 run` on this one continues the run it records"
            )
        # the store holds every control input and job output lands beside it: owner-only by
        # explicit mode, not by umask
        os.chmod(run_root, 0o700)
        return _genesis(
            catalog,
            run_root,
            lock,
            clock=clock,
            adapters=adapters,
            scheduler=scheduler,
            hold_open=hold_open,
            launched=launched,
            non_idempotent=non_idempotent,
            machines=machines,
            settings=settings,
            source_capture=source_capture,
        )
    except BaseException:
        lock.release()
        raise


def _genesis(
    catalog: CatalogIR,
    run_root: Path | None,
    lock: LeaderLock | None,
    *,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    scheduler: Scheduler | None,
    hold_open: bool,
    launched: RuntimeProfile | None,
    non_idempotent: Mapping[str, str] | None,
    machines: Executor | None,
    settings: EngineSettings | None,
    source_capture: Mapping[str, str] | None = None,
) -> Engine:
    at = clock.now()
    _require_adapters(catalog, adapters, "genesis")
    _require_scheduler(catalog, scheduler, "genesis")
    profile = _derive_runtime_profile(scheduler, adapters, base=launched, declared=launched)
    drift = [] if launched is None else profile_drift(profile, launched)
    if drift:
        # the record must describe the machine that runs: a launch profile the wiring
        # disagrees with is refused before anything is durable
        raise EngineError(
            f"the launch profile disagrees with the engine's wiring on {', '.join(drift)}"
            " (period-model ss2.1)"
        )
    configuration = Configuration(
        catalog=catalog,
        profile=profile,
        non_idempotent=dict(non_idempotent or {}),
        source_capture=dict(source_capture or {}),
    )
    identity = {
        "state_version": str(STATE_MACHINE_VERSION),
        "baseline_id": str(uuid.uuid4()),
        "clock_domain": _clock_domain(clock),
        # the run root's identity, the first half of every process cookie (spec-restart §3)
        "root_id": uuid.uuid4().hex,
    }
    store: Store
    path: Path | None = None
    if run_root is None:
        from dsl41.runner_store import MemoryStore

        store = MemoryStore(identity=identity)
    else:
        for name in ("runs", "plans"):
            (run_root / name).mkdir(exist_ok=True)
        path = run_root / STORE_NAME
        store = SqliteStore(path, created_at=at, identity=identity)
        os.chmod(path, 0o600)
    journal = Journal(store, lock=lock, path=path)
    instance = uuid.uuid4().hex  # this engine start's token, recorded in `engine started`
    try:
        rev = store.put_config(configuration.to_bytes(), configuration.digest())
        journal.append(_config_applied(rev, configuration, journal, at))
        journal.engine("started", at, config_rev=rev, instance=instance)
        if lock is not None:
            lock.note(at=at)
    except BaseException:
        with contextlib.suppress(Exception):
            journal.detach()
        raise
    engine = Engine(
        catalog,
        clock=clock,
        adapters=adapters,
        journal=journal,
        run_root=run_root,
        scheduler=scheduler,
        hold_open=hold_open,
        profile=profile,
        fence=lock,
        semantics=switches_of(profile),
        machines=machines,
        settings=settings,
    )
    engine.config_rev = engine.trace_rev = rev
    engine.source_capture = dict(configuration.source_capture)
    engine.instance = instance
    engine.marked = frozenset(configuration.non_idempotent)
    engine.decisions.fallback = _decision_lookup(store)
    engine.lifecycle.replay_done()  # genesis has nothing to replay
    engine.started.set()  # genesis recorded `engine started` above
    return engine


def _config_applied(
    rev: int, configuration: Configuration, journal: Journal, at: datetime
) -> Record:
    """The `config` record of an applied rev: what the wire's `segment` carries."""
    return Record(
        at,
        "config",
        None,
        None,
        {
            "event": "applied",
            "rev": rev,
            "at": at.isoformat(),
            "catalog_hash": configuration.catalog_hash,
            "runtime_hash": configuration.runtime_hash,
            "state_machine_version": STATE_MACHINE_VERSION,
            "baseline_id": journal.baseline_id,
            "clock_domain": journal.clock_domain,
        },
    )


def _decision_lookup(store: Store) -> Any:
    """The decision index's fallback over the store: a request id's decision and the
    fingerprint it was admitted under, for decisions a snapshot covers (`Store.find_request`)."""
    from dsl41.runner_admission import ApplyResult, RequestCollision

    def lookup(request_id: str) -> tuple[ApplyResult, str] | None:
        # a `decision` record carries a request id in the store's unique column, and so does an
        # update's `config intent`: an input under an update's id reuses it (spec-reload)
        record = store.find_request(request_id)
        if record is None:
            return None
        if record.kind != "decision":
            raise RequestCollision(
                f"request_id {request_id!r} was admitted for an update: reuse an id only for"
                " an exact retry"
            )
        body = record.payload
        result = ApplyResult(
            index=int(body["index"]),
            request_id=str(body["request_id"]),
            decision=body["decision"],
            reason=body.get("reason"),
            code=body.get("code"),
            revisions=dict(body.get("revisions") or {}),
        )
        return result, str(body["fingerprint"])

    return lookup


async def resume_run(
    catalog: CatalogIR,
    run_root: Path,
    *,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    scheduler: Scheduler | None = None,
    hold_open: bool = False,
    grace_seconds: float | None = None,
    lock: LeaderLock | None = None,
    declared: RuntimeProfile | None = None,
    machines: Executor | None = None,
    settings: EngineSettings | None = None,
    stored_sources: bool = False,
) -> Engine:
    """Restart (module docstring). `grace_seconds`, when given, is the live ladder's TERM grace
    for this incarnation over the settings' `grace_term_s`. Returns an Engine with the catch-up's steps queued; the
    caller runs the loop, which takes them and lifts the barrier. Nothing here awaits a
    process, a wrapper, a spool or a timer (spec-restart, DL-433).

    A refused restart holds nothing: every task it started is cancelled and collected, the store
    is closed with `shutdown_during_startup` recorded when it was opened, and the lock is
    released last."""
    if grace_seconds is not None:
        settings = (settings or EngineSettings()).overridden(grace_term_s=grace_seconds)
    # acquire first (S6a, DL-99): a mutex taken after the first read or side effect is not a
    # mutex
    lock = lock or acquire_run_root(run_root)
    journals: list[Journal] = []
    engines: list[Engine] = []
    try:
        engine = await _resume_under_lock(
            catalog,
            run_root,
            lock,
            journals=journals,
            engines=engines,
            clock=clock,
            adapters=adapters,
            scheduler=scheduler,
            hold_open=hold_open,
            declared=declared,
            machines=machines,
            settings=settings,
            stored_sources=stored_sources,
        )
    except BaseException:
        for engine in engines:
            # never mask the refusal with the shutdown's own error
            with contextlib.suppress(Exception):
                await engine.shutdown()
        for journal in journals:
            with contextlib.suppress(Exception):
                if journal.writable:
                    journal.engine("shutdown_during_startup", clock.now())
            with contextlib.suppress(Exception):
                journal.detach()
        lock.release()
        raise
    return engine


async def _resume_under_lock(
    catalog: CatalogIR,
    run_root: Path,
    lock: LeaderLock,
    *,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    scheduler: Scheduler | None,
    hold_open: bool,
    declared: RuntimeProfile | None,
    journals: list[Journal],
    engines: list[Engine],
    machines: Executor | None,
    settings: EngineSettings | None,
    stored_sources: bool = False,
) -> Engine:
    """The refusals, the fold and the first reading, with the lock held (S6a)."""
    refuse_old_root(run_root)
    path = run_root / STORE_NAME
    if not path.exists():
        raise EngineError(
            f"{run_root}: no {STORE_NAME} to resume: `dsl41 run` starts a new run in this root"
        )
    try:
        store = SqliteStore(path)
    except StoreError as exc:
        raise EngineError(f"{path}: {exc}") from exc
    meta = store.meta()
    try:
        _check_state_version(meta, path)
        _check_root_id(meta, path)
        snapshot = _latest_snapshot(store, path)
        # the configuration the records open with, and the one the last `config applied` left
        first = snapshot.config_rev if snapshot is not None else first_rev(store)
        after = snapshot.seq if snapshot is not None else 0
        tail = list(store.read_from(after))
        rev = _applied_rev(tail, first)
        start = load_configuration(store, first, where=str(path))
        configuration = start if rev == first else load_configuration(store, rev, where=str(path))
    except BaseException:
        store.close()
        raise
    journal = Journal(
        store,
        lock=lock,
        path=path,
        open_table=snapshot.open if snapshot is not None else None,
        position=snapshot.position if snapshot is not None else None,
    )
    journal.writable = False  # nothing is written until every refusal has had its chance
    journals.append(journal)
    for seq, record in tail:
        journal.open.note(seq, record)
        journal.position.note(record)
    opened_update = None
    if stored_sources:
        if journal.open.update is not None:
            from dsl41.runner_update import reopen_plan

            opened_update = reopen_plan(run_root, str(journal.open.update["token"]))
        profile = _check_configuration(
            configuration, catalog, scheduler, adapters, declared, host_settings=True
        )
    elif rev == first_rev(store) and journal.open.update is None:
        profile = _check_configuration(configuration, catalog, scheduler, adapters, declared)
    else:
        # the store's configuration rows are the record; the estate given must be the one the
        # engine runs once step 4 is done (spec-reload, DL-424)
        profile, scheduler, adapters = _check_updated(
            configuration,
            rev,
            catalog,
            journal.open.update,
            run_root,
            scheduler,
            adapters,
            declared,
            clock,
        )
        catalog = configuration.catalog
    _check_clock_domain(meta, clock)
    _require_adapters(catalog, adapters, "resume")
    _require_scheduler(catalog, scheduler, "resume")
    last_at = journal.position.last_at
    # genesis appends the configuration before anything else, and a snapshot keeps the position
    assert last_at is not None
    os.chmod(run_root, 0o700)
    engine = Engine(
        catalog,
        clock=clock,
        adapters=adapters,
        journal=journal,
        run_root=run_root,
        scheduler=scheduler,
        hold_open=hold_open,
        profile=profile,
        fence=lock,
        semantics=switches_of(profile),
        machines=machines,
        settings=settings,
    )
    engines.append(engine)
    engine.config_rev = rev
    engine.source_capture = dict(configuration.source_capture)
    engine.trace_rev = first  # the trace starts at the snapshot, or at genesis
    engine.marked = frozenset(configuration.non_idempotent)
    engine.resume_at = last_at
    wires = [wire for _, record in tail if (wire := wire_record(record)) is not None]
    replay = _replay_into_engine(engine, wires, snapshot, start, path)
    if not clock.virtual:
        engine.frontiers = engine.frontiers.model_copy(
            update={"at": max(engine.frontiers.at or last_at, last_at)}
        )
        engine.now_admitted()
    journal.writable = True  # every refusal has had its chance (spec-restart §1)
    lock.note(at=clock.now())
    for job, rt in engine.oracle.store.job.items():
        if rt.run_number:
            engine._dispatched[job] = rt.run_number
    _fold_evidence(engine, snapshot, tail)
    # the fold: every open attempt instantiated before the loop's first step, then the first
    # reading, then the catch-up's steps in seq order and the owed work after them (§§2-6)
    steps = _adopt_open_attempts(engine, _interrupted_drain(tail) or frozenset(), last_at)
    scan = _first_reading(engine, tail, _interrupted_drain(tail) or frozenset())
    for step in steps:
        engine.catch_up(partial(step, scan))
    engine.catch_up(partial(_owed, engine, wires, replay, last_at))
    if journal.open.update is not None:  # the update in STOPPING, its filter holding
        engine.update.resume(journal.open.update, opened=opened_update)
    # the scheduler re-anchors now, so no tick missed while down fires late; the barrier
    # records the drops with `engine started` once the catch-up is done (§6, item 4)
    missed = _missed_ticks(engine, journal.position)
    engine.recovery = lambda: _record_start(engine, tail, missed)
    return engine


def active_configuration(run_root: Path) -> Configuration:
    """Read the last applied configuration, excluding an uncommitted row (DL-437)."""
    path = run_root / STORE_NAME
    try:
        opened = SqliteStore(path, read_only=True)
    except StoreError as exc:
        raise EngineError(f"{path}: {exc}") from exc
    with contextlib.closing(opened) as store:
        _check_state_version(store.meta(), path)
        _check_root_id(store.meta(), path)
        snapshot = _latest_snapshot(store, path)
        first = snapshot.config_rev if snapshot is not None else first_rev(store)
        after = snapshot.seq if snapshot is not None else 0
        rev = _applied_rev(list(store.read_from(after)), first)
        return load_configuration(store, rev, where=str(path))


def _check_state_version(meta: Mapping[str, str], path: Path) -> None:
    """Step 2: the store's state version is this binary's, named both ways when it is not."""
    found = meta.get("state_version")
    if found != str(STATE_MACHINE_VERSION):
        raise EngineError(
            f"{path}: state version {found}, this binary runs {STATE_MACHINE_VERSION}: drain"
            " and snapshot with the binary that wrote it, or start a fresh run root (DL-424)"
        )


def _check_root_id(meta: Mapping[str, str], path: Path) -> None:
    """The store names its run root: every process cookie starts with it (spec-restart §3)."""
    if not meta.get("root_id"):
        raise EngineError(
            f"{path}: the store has no root_id, which every process cookie names: start a fresh"
            " run root (spec-restart, DL-433)"
        )


def _check_clock_domain(meta: Mapping[str, str], clock: Clock) -> None:
    """A real run and a rehearsal are not one record: the clock is the store's domain."""
    domain = _clock_domain(clock)
    if meta.get("clock_domain") != domain:
        raise EngineError(
            f"clock-domain mismatch: the store is {meta.get('clock_domain')!r},"
            f" the restart clock is {domain!r}"
        )


def _latest_snapshot(store: Store, path: Path) -> Any:
    """Step 3's snapshot, refused when another state version wrote it."""
    stored = store.latest_snapshot()
    if stored is None:
        return None
    if stored.state_version != STATE_MACHINE_VERSION:
        raise EngineError(
            f"{path}: the snapshot at seq {stored.seq} has state version"
            f" {stored.state_version}, this binary runs {STATE_MACHINE_VERSION} (DL-424)"
        )
    return decode_snapshot(stored.seq, stored.data, where=str(path))


def _check_configuration(
    configuration: Configuration,
    catalog: CatalogIR,
    scheduler: Scheduler | None,
    adapters: Mapping[str, JobAdapter],
    declared: RuntimeProfile | None,
    *,
    host_settings: bool = False,
) -> RuntimeProfile:
    """Require the applied catalog and interpretation; main-config host settings may vary
    for this incarnation only (DL-437). Legacy launches require the whole profile (ss7)."""
    if configuration.catalog_hash != catalog_hash_v2(catalog):
        raise EngineError(
            "catalog hash mismatch: the estate changed since this store was written;"
            " start a fresh run root (no silent semantic drift, ss7)"
        )
    expected = configuration.profile
    if host_settings and declared is not None:
        expected = with_host_settings(expected, declared.as_machine, declared.machine_policy)
    derived = _derive_runtime_profile(scheduler, adapters, base=expected, declared=declared)
    drift = profile_drift(derived, expected)
    if drift:
        raise EngineError(
            f"runtime-profile mismatch on {', '.join(drift)}: the store runs another"
            " profile; start a fresh run root"
        )
    return expected


def _applied_rev(tail: list[tuple[int, Record]], first: int) -> int:
    """The configuration rev the last `config applied` of the tail names, else `first`."""
    rev = first
    for _, record in tail:
        if record.kind == "config" and record.payload.get("event") == "applied":
            rev = int(record.payload["rev"])
    return rev


def _check_updated(
    configuration: Configuration,
    rev: int,
    launched: CatalogIR,
    intent: Mapping[str, Any] | None,
    run_root: Path,
    scheduler: Scheduler | None,
    adapters: Mapping[str, JobAdapter],
    declared: RuntimeProfile | None,
    clock: Clock,
) -> tuple[RuntimeProfile, Scheduler | None, dict[str, JobAdapter]]:
    """A restart of a run root an update touched runs the store's last applied configuration,
    never the estate's files (DL-424). The estate the launch names must be the one the engine
    runs once the resumed update applies: the open update's candidate, else the applied catalog,
    compared job by job under the settings the launch declares (`reload_diff.diff`), so a file
    path or a comment does not count and a definition or a setting does. The launch's wiring
    takes the exact path's runtime-profile check against that estate's profile (the machine
    identity and policy, the FW window, the zone, the map and the switches), so a changed
    value is refused by name as it is on a root no update touched. The scheduler is built
    afresh over the configuration and the adapters are refit to its switches, so the engine
    runs one profile; step 4 then replaces them as the update does.

    Returns the profile, the scheduler and the adapters the engine runs."""
    from dsl41.reload_diff import diff
    from dsl41.runner_update import profile_for, reopen_plan, settings_of

    profile = configuration.profile
    target, target_settings = configuration.catalog, settings_of(profile, configuration.catalog)
    if intent is not None:
        estate = reopen_plan(run_root, str(intent["token"])).estate
        target, target_settings = estate.catalog, dict(estate.settings)
    given = target_settings if declared is None else settings_of(declared, launched)
    moved = diff(target, launched, target_settings, given)
    if moved.added or moved.removed or moved.changed:
        raise EngineError(
            "catalog hash mismatch: the estate changed since this store was written"
            f" (configuration rev {rev}): reread and update once the engine runs, or launch"
            " with the estate it runs (no silent semantic drift, ss7)"
        )
    expected = profile_for(target_settings, profile)
    derived = _derive_runtime_profile(scheduler, adapters, base=expected, declared=declared)
    drift = profile_drift(derived, expected)
    if drift:
        raise EngineError(
            f"runtime-profile mismatch on {', '.join(drift)}: the store runs another"
            " profile; start a fresh run root"
        )
    wired = refit_adapters(adapters, switches_of(profile))
    if scheduler is not None:
        # the launch wired it from its own estate; a restart re-anchors it at the durable
        # frontier anyway (the time jump), so it is built afresh over the configuration
        scheduler = Scheduler(
            configuration.catalog,
            start=clock.now(),
            default_tz=profile.default_tz,
            tz_aliases=tz_aliases_of(profile),
            semantics=switches_of(profile),
        )
    return profile, scheduler, wired


def _replay_into_engine(
    engine: Engine,
    wires: list[dict[str, Any]],
    snapshot: Any,
    start: Configuration,
    path: Path,
) -> Any:
    """Step 3: restore the snapshot's rows under the configuration they were taken under,
    replay the tail's inputs, swapping to each configuration an update applied where its
    `segment` stands, and install the log's position and decisions on the engine."""
    assert engine.journal is not None
    store = engine.journal.store
    if snapshot is not None:
        engine.oracle = restore_oracle(
            start.catalog, snapshot.rows, **oracle_reading(start.profile)
        )
    elif start.catalog_hash != catalog_hash_v2(engine.oracle.catalog):
        engine.oracle = Oracle(start.catalog, **oracle_reading(start.profile))
    if snapshot is not None:
        engine.restored_runs = snapshot.runs
        engine.restored_rows = dict(snapshot.rows.jobs)
        # the trace a restart serves starts at its snapshot, and its first line says so
        since = snapshot.position.last_at
        assert since is not None  # a snapshot follows genesis's configuration record
        engine.oracle.mark(
            since,
            "RESTART",
            f"trace since the snapshot at {since.isoformat()}; `dsl41 runs` has the history",
        )
    try:
        replay = replay_inputs(
            engine.oracle,
            wires,
            machines=engine.machines,
            frontiers=snapshot.frontiers if snapshot is not None else None,
            rev=snapshot.config_rev if snapshot is not None else None,
            configs=lambda rev: load_configuration(store, rev, where=str(path)),
        )
    except ReplayFault as exc:
        # refuse, don't degrade: the same build raises at the same input on every restart
        raise EngineError(
            f"{path.parent}: resume stopped: {exc}. Deploy a build that fixes the fault,"
            " or roll back to the release that wrote this store",
            code=exc.code,
        ) from exc
    engine.frontiers = replay.frontiers
    engine.decisions = replay.decisions
    engine.decisions.fallback = _decision_lookup(engine.journal.store)
    return replay


# ---------------------------------------------------------------- the fold and the first reading


def _adopt_open_attempts(
    engine: Engine, stopping: frozenset[str], last_at: datetime
) -> list[Callable[[_procid.Ours | None], None]]:
    """Every open attempt in seq order, before the loop's first step (spec-restart §2, §5): a
    command attempt is instantiated in the attempt tier, in SPAWNING, or in ENDED with its
    committed outcome when it is closing. Answers the catch-up step of each, in the same order:
    an observation, a cleanup, or a file watch taken up from its log. An attempt the engine
    left taking a stop keeps that stop: the cause the open table carries, or `engine_stop` for
    one an interrupted drain named (`stopping`, DL-426). The fold writes nothing."""
    assert engine.journal is not None
    table = engine.journal.open
    steps: list[Callable[[_procid.Ours | None], None]] = []
    for attempt in sorted(table.attempts.values(), key=lambda a: a.seq):
        job_ir = engine.oracle.catalog.jobs.get(attempt.job)
        if job_ir is not None and job_ir.job_type == "FW":
            steps.append(partial(_watch, engine, job_ir, attempt, last_at))
            continue
        facts = _facts_of(attempt)
        if attempt.closing:
            # a snapshot older than the outcome's kind reads as a stop's (DL-431)
            outcome = AttemptOutcome(cast("Any", attempt.outcome or "stopped"), cause=attempt.stop)
            owed = attempt.attempt in table.plans
            engine.adopt_attempt(facts, outcome, plan_done=not owed, restart=True)
            steps.append(partial(_cleanup, engine, attempt))
        else:
            engine.adopt_attempt(facts, restart=True)
            stop = attempt.stop or (ENGINE_STOP if attempt.attempt in stopping else None)
            steps.append(partial(_observe, engine, attempt, stop))
    return steps


def _facts_of(attempt: OpenAttempt) -> AttemptFacts:
    # a restart holds a run root, and every attempt dispatched under one names its spool
    assert attempt.spool is not None
    return AttemptFacts(
        attempt.attempt,
        attempt.job,
        attempt.run_number,
        attempt.ordinal,
        Path(attempt.spool),
        dict(attempt.env),
        attempt.after,
        exited=True,  # its wrapper was the previous engine's
        closing=attempt.closing,
    )


def _first_reading(
    engine: Engine, tail: list[tuple[int, Record]], stopping: frozenset[str]
) -> _procid.Ours | None:
    """The cookie scan, before the loop's first step, where the cookie can be read (spec-restart
    §3): one scan of every process carrying a cookie of this run root, then, for each open
    command attempt, a reading of what is left of it through `left` with that shared scan, as
    the held poll reads, so a group a cookie member proves stays proven when that member exits.
    It finishes what the engine's death ended: what the reading found of an attempt with no
    outcome whose spool holds no natural record (none, a stop's, or a spool that does not
    read), or of a closing attempt but a `natural` one under `process-leak=leak`, is TERMed,
    the first rung of the attempt's ladder, whose grace and KILL run in the loop. What ended by
    its own record follows the live rule at its observation. A closing `natural` one's under
    `leak` is left running and named in one warning; a process of no open attempt is reported
    once and left alone. A scan that failed reads nothing and TERMs nothing: each attempt's
    live readings then hold it while they fail. None where no scan can be made."""
    assert engine.journal is not None
    scan = engine.stops.scan()
    if scan is None or scan.error is not None:
        return scan
    table = engine.journal.open.attempts
    leak = engine.stops.leak_policy == "leak"
    for attempt in sorted(table.values(), key=lambda a: a.seq):
        if not engine.attempts.knows(attempt.attempt):
            continue  # a file watch carries no cookie
        run_id = attempt.attempt
        spawn = _spawn_record(attempt)
        if isinstance(spawn, dict):
            engine.stops.note_boot(spawn)
        group = command_group(spawn if isinstance(spawn, dict) else None)
        look = engine.stops.left(*(group or (NO_GROUP, run_id)), run_id, scan)
        if not look.pids:
            continue
        if attempt.closing and attempt.outcome == "natural" and leak:
            best_effort_report(
                f"dsl41: {attempt.job}.{attempt.run_number}.{attempt.ordinal}: leaked:"
                f" {len(look.pids)}, left running (pids {_pids(look.pids)})"
            )
        elif not attempt.closing and _ended_by_its_record(attempt, stopping):
            # the scan finishes what the engine's death ended; what ended by its own record
            # follows the live rule at its observation (spec-restart §3, DL-433)
            continue
        else:
            for pid, start in look.pids.items():
                if _procid.signal_ours(pid, start, signal.SIGTERM, pidfd=engine.stops.pidfd):
                    engine.stops.termed.add(run_id)
    named = {
        str(record.payload["attempt"]): (record.payload["job"], record.payload["run_number"])
        for _, record in tail
        if record.kind == "dispatch"
    }
    strangers: dict[str, list[int]] = {}
    for pid, (run_id, _start) in sorted(scan.pids.items()):
        if run_id not in table:
            strangers.setdefault(run_id, []).append(pid)
    for run_id, pids in strangers.items():
        job, run = named.get(run_id, ("?", "?"))
        best_effort_report(
            f"dsl41: {job}.{run}: run id {run_id} has no open attempt; its processes are left"
            f" alone (pids {_pids(pids)})"
        )
    return scan


def _ended_by_its_record(attempt: OpenAttempt, stopping: frozenset[str]) -> bool:
    """Whether an open attempt's spool holds a natural record: one its wrapper wrote with no
    stop of the attempt in progress (spec-restart §3). A spool that does not read holds none.
    The observation reads the spool again; a record is written once."""
    assert attempt.spool is not None
    records = _records(Path(attempt.spool), attempt)
    if isinstance(records, str) or records[1] is None:
        return False
    stop = attempt.stop or (ENGINE_STOP if attempt.attempt in stopping else None)
    record = exit_record_from_status(records[1], stop_in_progress=stop is not None)
    return record.terminated_by is None


def _pids(pids: Iterable[int]) -> str:
    return " ".join(str(pid) for pid in sorted(pids)) or "?"


# ---------------------------------------------------------------- the catch-up's steps


def _record(spool: Path, name: str, attempt: OpenAttempt) -> dict[str, Any] | None | str:
    """One spool record of an attempt: None when absent, unreadable as JSON, or naming another
    (job, run) (DL-178y); or why it does not read, for a quarantine: it names another run id
    than the dispatch bound (DL-118), or it cannot be read (spec-restart §7)."""
    path = spool / name
    try:
        path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{name} unreadable: {exc.strerror or exc}"
    doc = load_json(path)
    if doc is not None and doc.get("run_id") != attempt.attempt:
        return (
            f"{name} reports run_id {doc.get('run_id')!r} but the dispatch record bound"
            f" {attempt.attempt!r} (DL-118)"
        )
    return doc if spool_names_run(doc, job=attempt.job, run_number=attempt.run_number) else None


def _spool_listing(spool: Path) -> str | None:
    """Why a spool directory cannot be listed, or None (a missing one is an empty spool)."""
    try:
        os.listdir(spool)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"spool unreadable: {exc.strerror or exc}"
    return None


def _records(spool: Path, attempt: OpenAttempt) -> tuple[Any, Any] | str:
    """An attempt's `spawn.json` and `status.json` (`_record`), or why the records do not read,
    for a quarantine: the spool directory, or either record (spec-restart §7)."""
    listing = _spool_listing(spool)
    if listing is not None:
        return listing
    spawn, status = _record(spool, "spawn.json", attempt), _record(spool, "status.json", attempt)
    for doc in (spawn, status):
        if isinstance(doc, str):
            return doc
    return spawn, status


def _spawn_record(attempt: OpenAttempt) -> dict[str, Any] | None | str:
    """An attempt's `spawn.json` alone: the identity its cleanup and the first reading need,
    whatever its `status.json` says (`_record`)."""
    assert attempt.spool is not None
    spool = Path(attempt.spool)
    return _spool_listing(spool) or _record(spool, "spawn.json", attempt)


def _watch_split(attempt: OpenAttempt) -> str | None:
    """Why a file watch's log does not read as this attempt's (DL-118, spec-restart §7), or
    None."""
    assert attempt.spool is not None
    try:
        watch = read_watch_log(Path(attempt.spool))
    except EngineError as exc:
        return str(exc)
    if watch is not None and watch.run_id != attempt.attempt:
        return (
            f"watch.jsonl reports run_id {watch.run_id!r} but the dispatch record bound"
            f" {attempt.attempt!r} (DL-118)"
        )
    return None


def _watch(
    engine: Engine, job_ir: JobIR, attempt: OpenAttempt, last_at: datetime, _scan: object
) -> None:
    """One open file watch, as a catch-up step in seq order (spec-restart §2): taken up from its
    log, or quarantined when its log does not read as this attempt's (§7)."""
    split = _watch_split(attempt)
    if split is None:
        _resume_watch(engine, job_ir, attempt, last_at)
        return
    engine.adopt_attempt(_facts_of(attempt), observed=False, restart=True)
    engine.quarantine(attempt.attempt, split)


def _observe(engine: Engine, attempt: OpenAttempt, stop: str | None, scan: object) -> None:
    """One open attempt without an outcome, observed once (spec-restart §4): its spool read
    once, then `spawn_recorded`, the recorded first stop before any status, so an exit the
    previous engine's TERM caused is `stopped` (DL-426), and the end: the wrapper's record, or
    `engine_died` when there is none. The live record path does the rest: the reading of what
    is left, the ladder on it, the outcome and its plan; a reading that fails holds it as
    live. Records that do not read quarantine the attempt, the pids the first reading found
    named (spec-restart §7)."""
    assert attempt.spool is not None
    run_id = attempt.attempt
    records = _records(Path(attempt.spool), attempt)
    if isinstance(records, str):
        found = scan.pids if isinstance(scan, _procid.Ours) else {}
        pids = [pid for pid, (run, _start) in found.items() if run == run_id]
        engine.quarantine(run_id, f"{records} (pids {_pids(pids)})" if pids else records)
        return
    spawn, status = records
    tier = engine.attempts
    if spawn is not None:
        engine.stops.note_boot(spawn)
        tier.spawn_recorded(run_id, spawn)  # a record never proves a group: no first reading
    if stop is not None:
        tier.restored_stop(run_id, stop)  # the stop outlived the engine that sent it
    if status is not None:
        tier.status_recorded(run_id, status)
    else:
        tier.engine_died(run_id)


def _cleanup(engine: Engine, attempt: OpenAttempt, _scan: object) -> None:
    """A closing attempt, restored ENDED with its committed outcome: one reading of what is left
    and the ladder on it, as at a live record (spec-restart §5). It reads `spawn.json` alone,
    its identity; one that does not read is reported and the cookie's processes are then all
    that is read. Nothing left closes it (`ladder_done`); a `natural` one's processes under
    `process-leak=leak` are reported and it closes; a ladder's end closes it; a survivor of
    the KILL, or a reading that fails, holds it as live."""
    run_id = attempt.attempt
    spawn = _spawn_record(attempt)
    if isinstance(spawn, str):
        best_effort_report(f"dsl41: {attempt.job}.{attempt.run_number}: {spawn}")
        spawn = None
    tier = engine.attempts
    group = command_group(spawn)
    if spawn is not None:
        engine.stops.note_boot(spawn)
        tier.spawn_recorded(run_id, spawn)  # ENDED takes no event; the tier keeps the group
    if group is not None:
        # its ladder was running when the engine died: a member left in its group outlived a
        # stop, as live (DL-426)
        engine.stops.note_laddered(group[0])
    tier.cleanup(run_id)  # the reading is the live one, after the first reading's TERM
    if not (tier.ladder_running(run_id) or tier.held(run_id) or run_id in engine.held_groups):
        engine._ladder_ended(run_id)  # nothing to stop: closed now


def _owed(engine: Engine, wires: list[dict[str, Any]], replay: Any, last_at: datetime) -> None:
    """The catch-up's owed work, once every observation and cleanup is taken (spec-restart §6):
    the plans the store holds unfinished, in seq order, then the pending starts; then the first
    look at a resumed update's stop set (DL-432)."""
    engine.perform_owed_plans()
    _discharge_pending(engine, wires, replay, last_at)
    engine.update.look()


def _interrupted_drain(tail: list[tuple[int, Record]]) -> frozenset[str] | None:
    """The attempts the last incarnation's drain was stopping, when it never reached a shutdown
    record: the replayed lifecycle, not the last record's kind (spec-drain). A start after the
    draining record that recorded `drain_interrupted` closes it; a `shutdown_during_startup`
    does not, so a restart that failed early reports the drain again."""
    stopping: frozenset[str] | None = None
    for _, record in tail:
        if record.kind != "engine":
            continue
        event = record.payload.get("event")
        if event == "draining":
            stopping = frozenset(str(a) for a in record.payload.get("unsettled") or ())
        elif event in ("started", "clean_shutdown", "unclean_shutdown"):
            stopping = None
    return stopping


def _fold_evidence(engine: Engine, snapshot: Any, tail: list[tuple[int, Record]]) -> None:
    """The `Interrupted` evidence each held job shows until its next start (spec-crash): the
    snapshot's, then the tail's hold plans and starts in seq order."""
    if snapshot is not None:
        engine.evidence = {job: dict(evidence) for job, evidence in snapshot.evidence.items()}
    for _, record in tail:
        body = record.payload
        if record.kind == "follow_up" and isinstance(body.get("evidence"), dict):
            engine.evidence[str(body["job"])] = {
                **body["evidence"],
                "run_number": body["run_number"],
            }
        elif record.kind == "dispatch":
            held = engine.evidence.get(str(body["job"]))
            if held is not None and held["run_number"] != body["run_number"]:
                del engine.evidence[str(body["job"])]


def _resume_watch(engine: Engine, job_ir: JobIR, attempt: OpenAttempt, last_at: datetime) -> None:
    """One open FW watch, from its spool (period-model ss2.2). A watcher has no attempt to rerun:
    it resumes only while the oracle's current run for its job is this run in STARTING or
    RUNNING; a complete log is fed through the stale-completion gate, never re-watched; a watch
    whose run ended is closed with no plan."""
    from dsl41.runner_adapters import FileWatcherAdapter
    from dsl41.semantics import fw_existence_immediate

    assert engine.journal is not None
    job, run = job_ir.name, attempt.run_number
    spool = Path(attempt.spool) if attempt.spool is not None else None
    watch = read_watch_log(spool) if spool is not None else None
    adapter = engine.adapters.get("FW")
    if adapter is None:
        raise AssertionError(
            "_require_adapters already refuses a restart with an FW job and no FW adapter"
            " wired, so adapter cannot be None here"
        )
    # DL-258: an `immediate` watch with no minimum size is complete when the run's first poll
    # qualifies, and only the first: `watch_seq == 2` is exactly one poll line
    immediate = isinstance(adapter, FileWatcherAdapter) and fw_existence_immediate(
        job_ir, adapter.existence
    )
    immediate_first_poll = immediate and watch is not None and watch.watch_seq == 2
    if watch is not None and (watch.complete or (immediate_first_poll and watch.qualifying)):
        # PR-34a: the engine died between the completing poll and the STATUS input: the
        # completion is fed from the log, never re-polled
        if watch.last_at is None:
            raise AssertionError(
                "a complete or immediately-qualifying watch has at least one poll"
                " line, so last_at cannot be None (WatchLog invariant)"
            )
        # the outcome goes the live way, so an update in STOPPING may keep it (spec-restart §6)
        ended = ExitRecord(
            exit_code=0, ended_at=watch.last_at.isoformat(), wrapper_outcome="exited"
        )
        engine.adopt_attempt(_facts_of(attempt), observed=False, restart=True)
        engine.record_outcome(attempt.attempt, AttemptOutcome("natural", record=ended))
        return
    rt = engine.oracle.store.job.get(job)
    if rt is None or rt.run_number != run or rt.status not in LIVE:
        # DL-242: only a live row is watched again; the oracle ended the run, so plan `none`
        engine.adopt_attempt(_facts_of(attempt), observed=False, restart=True)
        engine.record_outcome(attempt.attempt, AttemptOutcome("stopped", cause="run ended"))
        return
    # an idempotent read: the adapter reconstructs progress from the log and appends no second
    # `start` line, under the attempt's recorded id (DL-118)
    engine._launch(job_ir, run, adapter, run_id=attempt.attempt, spool=spool)


# ---------------------------------------------------------------- the owed work and the barrier


def _discharge_pending(
    engine: Engine, wires: list[dict[str, Any]], replay: Any, last_at: datetime
) -> None:
    """Step 7: every pending dispatch is deferred to the loop, which dispatches it while its run
    is still current. A start the replayed tail decided with no `dispatch` record (the previous
    engine died between the decision and the record, DL-102) is made a `dispatch_pending` first,
    so it is durable until its `dispatch` commits. A run that already has an attempt, open or
    dispatched in the tail, a pending record, or a replacement its plan queued is not started
    again."""
    assert engine.journal is not None
    table = engine.journal.open
    owed = sorted(table.pending.values(), key=lambda p: p.seq)
    for pending in owed:
        engine.defer(Deferred(pending.job, pending.run_number, pending=pending.seq), after=last_at)
    covered = set(dispatched_runs(wires)) | {(p.job, p.run_number) for p in owed}
    covered |= {(a.job, a.run_number) for a in table.attempts.values()}
    covered |= {
        (plan.job, plan.run_number)
        for plan in table.plans.values()
        if any(step["step"] == "dispatch" for step in plan.steps)
    }
    at = engine.clock.now()
    for job, rt in engine.oracle.store.job.items():
        key = (job, rt.run_number)
        if rt.status not in LIVE or key in covered or key not in replay.spawns:
            continue
        scheduled = rt.status_at if rt.status_at is not None else at
        seq = engine.journal.append(pending_record(job, rt.run_number, scheduled, at))
        engine.defer(Deferred(job, rt.run_number, pending=seq), after=last_at)


def _record_start(
    engine: Engine, tail: list[tuple[int, Record]], missed: list[tuple[Event, str]]
) -> None:
    """The barrier's record, run by the loop once the catch-up is done: `drain_interrupted` when
    the last incarnation's records show a `draining` after its last start with no shutdown
    record, `engine started`, and the drop of each scheduler tick missed while down (E9)."""
    assert engine.journal is not None
    at = engine.clock.now()
    draining = _interrupted_drain(tail) is not None
    if draining:
        engine.journal.engine("drain_interrupted", at)
    engine.journal.engine("started", at, config_rev=engine.config_rev, instance=engine.instance)
    for tick_ev, reason in missed:
        engine.journal.drop(tick_ev, reason)


def _missed_ticks(engine: Engine, position: Position) -> list[tuple[Event, str]]:
    """E9: the scheduler re-anchors at its durable frontier, inclusive, and every tick between
    it and now that the records did not adjudicate is dropped, never fired late (DL-45,
    DL-166). The drops go on `engine.drops` now and into the store at the barrier."""
    scheduler = engine.scheduler
    if scheduler is None or position.frontier is None:
        return []
    # always inclusive: with several jobs at one instant, a crash between the siblings' inputs
    # leaves the frontier at the tick with a sibling unrecorded
    scheduler.reset(position.frontier)
    now = engine.clock.now() if engine.clock.virtual else engine.now_admitted()
    sweep_upto = max(now, position.frontier)
    missed: list[tuple[Event, str]] = []
    for tick_ev in scheduler.pop_due(sweep_upto):
        if (tick_ev.job(), tick_ev.at.isoformat()) in position.ticks:
            continue
        reason = "scheduler tick missed while the engine was down; not fired late"
        tick_ev.source = "scheduler"
        missed.append((tick_ev, reason))
    engine.drops.extend(missed)  # PENDING: E9
    return missed
