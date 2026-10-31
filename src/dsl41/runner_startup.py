"""Taking possession of a run root: genesis and restart (DL-74, DL-106, DL-424).

The Engine is the single-writer loop over a live estate. What lives here runs once per
incarnation, before that loop exists: it creates or opens the run root's store under its lock,
restores and replays it, settles what the last incarnation left open, and hands back an Engine
ready to run. The name avoids "lifecycle", which DL-42 gives to the wrapper tier.

A run root holds `engine.db` (the store), `leader.lock`, `engine.toml`, `runs/` (the wrapper
spools) and `plans/`.

Restart follows spec-wiring 1b's eight steps:

1. take `flock leader.lock`, refusing with the holder named;
2. open the store; a `state_version` other than this binary's refuses, naming both;
3. load the latest snapshot and replay the `input` records after it; rebuild the open table
   from the records after it;
4. restore an update the last incarnation left in STOPPING (`config intent` with no `config
   applied`, `runner_update.EngineUpdate.resume`): its dispatch filter holds from here; step 5
   settles its attempts like any open one, and it applies as a step of the loop once they are
   down, so no job of its plan dispatches and no outcome it keeps commits before that;
5. settle every open attempt through the attempt chart: the attempt is instantiated under its
   run id and fed what its spool supports (`spawn.json`, `status.json`, a wrapper still writing
   its record, a command that outlived its wrapper and gets the ladder); its `outcome` is
   appended with its `follow_up` plan (`runner_attempts.follow_up_plan`);
6. finish every plan with an unfinished step, in seq order, without reclassifying it;
7. discharge every pending dispatch whose run is still the oracle's current run in STARTING;
   a start the tail decided with no `dispatch` becomes a `dispatch_pending` first;
8. record `engine started` (and `drain_interrupted` when the last incarnation was draining),
   and skip and report the scheduler ticks missed while down (E9). The loop runs this step once
   steps 6 and 7 have taken effect (`Engine.recovery`); the caller binds the control socket
   after it (`Engine.started`).

The plans of step 5 follow the interruption rule (spec-wiring 2b): a natural end or a failed
launch completes; a stopped or unobservable attempt is dropped when its job left the catalog or
the oracle ended its run, held when its job is named in `non-idempotent.toml` (FAILURE with
cause `interrupted`, then ON_HOLD), and otherwise run again as a new attempt of the same run with
the same invocation. Steps 6 and 7 feed through the engine's queue and launch through its
deferred dispatch, so the reconciliation inputs and the overdue timers apply first (DL-425); a
start one of those inputs decides is owed the same way. The engine lifecycle leaves STARTING
after step 7 (`replay_done`). A restart from a snapshot opens its trace with a `RESTART` line:
the trace covers what follows the snapshot.

`start_run` is genesis: an empty store has nothing to replay, settle or dispatch.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from dsl41 import runner_procid as _procid
from dsl41.engine_charts import ENGINE_STOP, WRAPPER_LOST
from dsl41.ir import CatalogIR, JobIR
from dsl41.reload_diff import catalog_hash_v2
from dsl41.oracle import Oracle
from dsl41.oracle_state import LIVE, Event
from dsl41.runner import (
    Deferred,
    Engine,
    adapter_switch,
    feed_step,
    outcome_record,
    plan_record,
    refit_adapters,
)
from dsl41.runner_adapters import (
    JobAdapter,
    load_json,
    read_watch_log,
    spool_names_run,
    spool_record,
)
from dsl41.runner_attempts import command_group
from dsl41.runner_clock import Clock, EngineError
from dsl41.runner_config import (
    RECONCILE_SETTLE_S,
    Configuration,
    EngineSettings,
    RuntimeProfile,
    oracle_reading,
    profile_drift,
    switches_of,
    to_us,
    tz_aliases_of,
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
    ladder_done_record,
    load_configuration,
    pending_record,
    replay_inputs,
    restore_oracle,
    wire_record,
)
from dsl41.runner_ledger import STATE_MACHINE_VERSION, LeaderLock, acquire_run_root
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_stops import COMMAND_SURVIVED, AttemptFacts
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
        catalog=catalog, profile=profile, non_idempotent=dict(non_idempotent or {})
    )
    identity = {
        "state_version": str(STATE_MACHINE_VERSION),
        "baseline_id": str(uuid.uuid4()),
        "clock_domain": _clock_domain(clock),
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
    instance = uuid.uuid4().hex  # the engine's token, the first half of its cookies
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
    settle_seconds: float | None = None,
    grace_seconds: float | None = None,
    lock: LeaderLock | None = None,
    declared: RuntimeProfile | None = None,
    machines: Executor | None = None,
    settings: EngineSettings | None = None,
) -> Engine:
    """Restart (module docstring). Returns an Engine with the reconciliation inputs queued and
    the owed dispatches deferred; the caller runs the loop to process them and continue.

    A refused restart holds nothing: every task it started is cancelled and collected, the store
    is closed with `shutdown_during_startup` recorded when it was opened, and the lock is
    released last."""
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
            settle_seconds=settle_seconds,
            grace_seconds=grace_seconds,
            declared=declared,
            machines=machines,
            settings=settings,
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
    settle_seconds: float | None,
    grace_seconds: float | None,
    declared: RuntimeProfile | None,
    journals: list[Journal],
    engines: list[Engine],
    machines: Executor | None,
    settings: EngineSettings | None,
) -> Engine:
    """Steps 2 to 8, with the lock held (S6a)."""
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
    if rev == first_rev(store) and journal.open.update is None:
        # a run root no update has touched runs the estate it was started with
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
    if not clock.virtual and last_at > clock.now():
        raise EngineError(
            f"the store is from the future ({last_at.isoformat()} > now): the machine"
            " clock moved backwards; refusing to feed non-decreasing time backwards"
        )
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
    engine.trace_rev = first  # the trace starts at the snapshot, or at genesis
    engine.marked = frozenset(configuration.non_idempotent)
    engine.resume_at = last_at
    wires = [wire for _, record in tail if (wire := wire_record(record)) is not None]
    replay = _replay_into_engine(engine, wires, snapshot, start, path)
    # the identity preflight runs before anything is written, so a refused restart has written
    # nothing
    _preflight_identities(engine)
    journal.writable = True
    lock.note(at=clock.now())
    for job, rt in engine.oracle.store.job.items():
        if rt.run_number:
            engine._dispatched[job] = rt.run_number
    windows = (
        settle_seconds if settle_seconds is not None else RECONCILE_SETTLE_S,
        grace_seconds if grace_seconds is not None else (settings or EngineSettings()).grace_term_s,
    )
    _fold_evidence(engine, snapshot, tail)
    stopping = _interrupted_drain(tail) or frozenset()
    if journal.open.update is not None:  # step 4: the update in STOPPING, its filter holding
        engine.update.resume(journal.open.update)
    await _settle_open_attempts(engine, last_at, windows, stopping)  # step 5
    # steps 6 and 7 owe their effects to the loop, which carries them out once the due work has
    # run; `recovery` is step 8, set in full below
    engine.recovery = lambda: None
    engine.adopt_plans()  # step 6
    engine.perform_plans()
    _discharge_pending(engine, wires, replay, last_at)  # step 7
    # the scheduler re-anchors now, so no tick missed while down fires late; step 8 records the
    # drops with `engine started` once the loop has carried out steps 6 and 7
    missed = _missed_ticks(engine, journal.position)
    engine.recovery = lambda: _record_start(engine, tail, missed)  # step 8
    return engine


def _check_state_version(meta: Mapping[str, str], path: Path) -> None:
    """Step 2: the store's state version is this binary's, named both ways when it is not."""
    found = meta.get("state_version")
    if found != str(STATE_MACHINE_VERSION):
        raise EngineError(
            f"{path}: state version {found}, this binary runs {STATE_MACHINE_VERSION}: drain"
            " and snapshot with the binary that wrote it, or start a fresh run root (DL-424)"
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
) -> RuntimeProfile:
    """The launch must run the configuration the store applied: the same catalog, and wiring
    that reads the same profile. A change is an update (3b), never a silent drift (ss7)."""
    if configuration.catalog_hash != catalog_hash_v2(catalog):
        raise EngineError(
            "catalog hash mismatch: the estate changed since this store was written;"
            " start a fresh run root (no silent semantic drift, ss7)"
        )
    derived = _derive_runtime_profile(
        scheduler, adapters, base=configuration.profile, declared=declared
    )
    drift = profile_drift(derived, configuration.profile)
    if drift:
        raise EngineError(
            f"runtime-profile mismatch on {', '.join(drift)}: the store runs another"
            " profile; start a fresh run root"
        )
    return configuration.profile


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
    runs once startup step 4 is done: the open update's candidate, else the applied catalog,
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
        # frontier anyway (step 8), so it is built afresh over the configuration
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


# ---------------------------------------------------------------- step 5


def _preflight_identities(engine: Engine) -> None:
    """Every open attempt's spool must name the attempt its `dispatch` record bound (DL-118).
    One sweep before anything is written, so a refusal leaves the store as it was."""
    assert engine.journal is not None
    for attempt in sorted(engine.journal.open.attempts.values(), key=lambda a: a.seq):
        # a restart holds a run root, and every attempt dispatched under one names its spool
        assert attempt.spool is not None
        spool = Path(attempt.spool)
        for name in ("spawn.json", "status.json"):
            doc = load_json(spool / name)
            if doc is not None:
                _refuse_identity_split(attempt, doc.get("run_id"), f"the spool's {name}")
        watch = read_watch_log(spool)
        if watch is not None:
            _refuse_identity_split(attempt, watch.run_id, "the spool's watch.jsonl")


def _refuse_identity_split(attempt: OpenAttempt, observed: object, source: str) -> None:
    """One key runs through the store and the spool (DL-118, PR-36a). A different id is a
    store/spool split: corruption, a spoofed record, or a directory from another estate, and
    acting on that process would act on a run the store never dispatched."""
    observed_id = str(observed) if observed is not None else None
    if observed_id != attempt.attempt:
        raise EngineError(
            f"{attempt.job}.{attempt.run_number}: {source} reports run_id {observed_id!r} but"
            f" the dispatch record bound {attempt.attempt!r} -- refusing to act on a run the"
            " store did not dispatch (DL-118)"
        )


async def _settle_open_attempts(
    engine: Engine,
    last_at: datetime,
    windows: tuple[float, float],
    stopping: frozenset[str] = frozenset(),
) -> None:
    """Step 5: every open attempt gets its `outcome` and its plan. A command attempt goes
    through the attempt chart (`_settle_attempt`), all of them together; a watch resumes from
    its log (`_resume_watch`). The plans wait for step 6. An attempt the engine left taking a
    stop is adopted with that stop: the cause the open table carries from the shutdown
    records, or `engine_stop` for one an interrupted drain named in its `draining` record
    (`stopping`), so an exit the stop's TERM caused is `stopped`, never `natural` (DL-426).
    An attempt an update in STOPPING stops is adopted with `update_stop` from the open table
    like any other (startup step 4 restored the update)."""
    assert engine.journal is not None
    engine.settling = True
    try:
        settling = []
        for attempt in sorted(engine.journal.open.attempts.values(), key=lambda a: a.seq):
            job_ir = engine.oracle.catalog.jobs.get(attempt.job)
            if job_ir is not None and job_ir.job_type == "FW":
                _resume_watch(engine, job_ir, attempt, last_at)
                continue
            if attempt.closing:  # its outcome is in; its ladder never ended
                settling.append(_finish_ladder(engine, attempt))
                continue
            # the stop the open table carries (the shutdown records'), cross-checked by the
            # list an interrupted drain left (DL-426)
            stop = attempt.stop or (ENGINE_STOP if attempt.attempt in stopping else None)
            settling.append(_settle_attempt(engine, attempt, *windows, stop=stop))
        await asyncio.gather(*settling)
    finally:
        engine.settling = False


async def _settle_attempt(
    engine: Engine,
    attempt: OpenAttempt,
    settle_seconds: float,
    grace_seconds: float,
    *,
    stop: str | None = None,
) -> None:
    """One open command attempt, instantiated in the attempt chart under its run id and fed the
    events its spool supports (spec-crash, Restart step 5). `spawn.json` is posted first. A
    `status.json` is the record. With none, a wrapper still alive in this boot gets
    `settle_seconds + grace_seconds` to write it (its own lifeline ladder is running) and, if
    it never does, the ladder runs on its command (`stop` with cause `wrapper_lost`); a wrapper
    gone is `wrapper_exited`, whose verdict reads the command's liveness. The step returns once
    the attempt ended, or once its stop could not be carried out (it stays STOPPING, unsettled,
    and the next restart reads its spool again)."""
    # a restart holds a run root, and every attempt dispatched under one names its spool
    assert attempt.spool is not None
    spool = Path(attempt.spool)
    facts = AttemptFacts(
        attempt.attempt,
        attempt.job,
        attempt.run_number,
        attempt.ordinal,
        spool,
        dict(attempt.env),
        attempt.after,
        exited=True,  # its wrapper was the previous engine's
    )
    engine.adopt_attempt(facts)
    run_id = attempt.attempt

    def read(name: str) -> dict[str, Any] | None:
        # the run id was checked by the preflight; a record naming another (job, run) is a
        # stranger's and is read as absent (DL-178y)
        doc = spool_record(spool / name, run_id)
        return doc if spool_names_run(doc, job=attempt.job, run_number=attempt.run_number) else None

    tier = engine.attempts
    spawn, status = read("spawn.json"), read("status.json")
    wrapper_alive = False
    if spawn is not None:
        engine.stops.note_boot(spawn)
        tier.spawn_recorded(run_id, spawn)
        wrapper_alive = _wrapper_alive(engine, spawn)
    if stop is not None:
        tier.stop(run_id, stop)  # the stop outlived the engine that sent it
    if status is None and wrapper_alive:
        assert spawn is not None  # `wrapper_alive` reads it
        deadline = time.monotonic() + settle_seconds + grace_seconds
        while status is None and wrapper_alive and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            status, wrapper_alive = read("status.json"), _wrapper_alive(engine, spawn)
    # every verdict that reads the attempt as dead reads the spool once more first: a wrapper
    # may write its record and die between the first read and the liveness check (DL-426)
    status = status or read("status.json")
    if status is not None:
        tier.status_recorded(run_id, status)
    elif wrapper_alive:
        tier.stop(run_id, WRAPPER_LOST)
    else:
        tier.wrapper_exited(run_id, spawn)
    await engine.settled_or_failed(facts)


async def _finish_ladder(engine: Engine, attempt: OpenAttempt) -> None:
    """An attempt whose outcome was committed while its ladder ran, and whose ladder the last
    engine never finished, before step 6 performs the plan: the restart stops what it finds
    left (`EngineStops.finish`). A recorded boot that is not this one proves nothing survived.
    Nothing left closes the attempt (`ladder_done`); anything left keeps it open and held,
    `command survived its KILL` (`held_at_restart`): no start of its job is launched beside it
    (DL-426). Under `process-leak=leak` a leak's ladder (a `natural` outcome) signals nothing:
    the processes left are named in a warning and the attempt closes, as the vendor would leave
    them. A stop the crash cut short is finished whatever the switch."""
    assert engine.journal is not None and attempt.spool is not None
    spawn = spool_record(Path(attempt.spool) / "spawn.json", attempt.attempt)
    group = command_group(spawn)
    if group is not None and spawn is not None and spawn.get("boot_id") == engine.stops.boot_id:
        engine.stops.note_boot(spawn)  # its wrapper is no process of the command
        engine.stops.note_cookie(attempt.attempt, attempt.env)
        if engine.stops.leak_policy == "leak" and attempt.outcome == "natural":
            look = engine.stops.left(*group, attempt.attempt)
            if look.alive:
                pids = " ".join(str(pid) for pid in sorted(look.pids)) or "?"
                best_effort_report(
                    f"dsl41: {attempt.job}.{attempt.run_number}.{attempt.ordinal}:"
                    f" leaked: {len(look.pids)}, left running (pids {pids})"
                )
        else:
            look = await engine.stops.finish(attempt.attempt, *group)
            if look.alive:
                note = look.reason if look.kind == "unknown" else COMMAND_SURVIVED
                engine.held_at_restart(attempt, group[0], note)
                return
    engine.journal.append(ladder_done_record(attempt.attempt, engine.clock.now()))
    engine.stops.forget(attempt.attempt)
    engine.update.cleaned(attempt.attempt)  # an update's stop of it is finished (step 4)


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


def _wrapper_alive(engine: Engine, spawn: dict[str, Any]) -> bool:
    """Whether the wrapper `spawn.json` names still runs in this boot (DL-42 item 5): the
    pid-reuse guard on its recorded pid and start token."""
    pid, token = spawn.get("wrapper_pid"), spawn.get("wrapper_start_time")
    return (
        spawn.get("boot_id") == engine.stops.boot_id
        and isinstance(pid, int)
        and isinstance(token, str)
        and _procid.verify_alive(pid, token)
    )


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
    at = engine.clock.now()
    if watch is not None and (watch.complete or (immediate_first_poll and watch.qualifying)):
        # PR-34a: the engine died between the completing poll and the STATUS input: the
        # completion is fed from the log, never re-polled
        if watch.last_at is None:
            raise AssertionError(
                "a complete or immediately-qualifying watch has at least one poll"
                " line, so last_at cannot be None (WatchLog invariant)"
            )
        payload: dict[str, object] = {
            "job": job,
            "run_number": run,
            "exit_code": 0,
            "ended_at": watch.last_at.isoformat(),
        }
        engine.journal.append(
            outcome_record(attempt.attempt, job, run, "natural", payload, at),
            plan_record(attempt.attempt, job, run, "complete", [feed_step(payload)], at),
        )
        return
    rt = engine.oracle.store.job.get(job)
    if rt is None or rt.run_number != run or rt.status not in LIVE:
        # DL-242: only a live row is watched again
        engine.journal.append(
            outcome_record(attempt.attempt, job, run, "stopped", {}, at, cause="run ended"),
            plan_record(attempt.attempt, job, run, "none", [], at, reason="run ended"),
        )
        return
    # an idempotent read: the adapter reconstructs progress from the log and appends no second
    # `start` line, under the attempt's recorded id (DL-118)
    engine._launch(job_ir, run, adapter, run_id=attempt.attempt, spool=spool)


# ---------------------------------------------------------------- steps 6 to 8


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
    """Step 8, run by the loop once steps 6 and 7 have taken effect: `drain_interrupted` when
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
    DL-166). The drops go on `engine.drops` now and into the store at step 8."""
    scheduler = engine.scheduler
    if scheduler is None or position.frontier is None:
        return []
    # always inclusive: with several jobs at one instant, a crash between the siblings' inputs
    # leaves the frontier at the tick with a sibling unrecorded
    scheduler.reset(position.frontier)
    sweep_upto = max(engine.clock.now(), position.frontier)
    missed: list[tuple[Event, str]] = []
    for tick_ev in scheduler.pop_due(sweep_upto):
        if (tick_ev.job(), tick_ev.at.isoformat()) in position.ticks:
            continue
        reason = "scheduler tick missed while the engine was down; not fired late"
        tick_ev.source = "scheduler"
        missed.append((tick_ev, reason))
    engine.drops.extend(missed)  # PENDING: E9
    return missed
