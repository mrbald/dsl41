"""Taking possession of a run root (ss7): genesis, resume, and the barrier (DL-74, DL-106).

The Engine is the single-writer loop over a live estate. What lives here runs once per
incarnation, before that loop exists: it creates or claims a run root, takes leadership of it,
replays the log, reconciles the estate against the world, and hands back an Engine ready to run.
The two halves share exactly one object, the one this half constructs. The name avoids
"lifecycle", which DL-42 gives to the wrapper and supervisor tier.

- Resume (ss7): refuse on catalog-hash or clock-domain mismatch, replay inputs through a fresh
  Oracle, seed the ghost-run gate so replayed starts never respawn, then reconcile from the
  spool ladder: live wrapper -> settle window; status.json -> inject the real completion at
  max(ended_at, last journal at) with the true ended_at in the payload; verified command group
  orphaned by a dead wrapper -> kill it, TERMINATED "wrapper lost; killed at resume" (a kill
  that happened); nothing -> FAILURE exit_status_unobservable (PENDING: E7). First, an attempt
  the previous engine admitted and never decided gets its decision record, with the effects it
  implies (DL-315). A start with no trace anywhere is then decided by its own SPAWN (DL-102): a
  PENDING one is an intent the previous leader never delivered and is re-driven; a RETIRED one,
  or none, means the live engine launched nothing for the run, and the row is left as it is; an
  APPLIED one that left no trace is FAILURE "dispatch lost to engine crash", because a run
  provably never started is never silently re-executed. FW watchers differ: polling is an
  idempotent read, so an applied watch with no trace is re-dispatched, and a supervised start
  with a bound id is replayed. A pending one is re-driven like any other start, so a held watch
  stays held. Reconciliation completions go through the ss4 stale gate like any adapter
  completion: if replay already reached a terminal state (say a term_run_time TERMINATED), the
  late real record is dropped and journaled, never silently overwritten.

The whole sequence is concurrency-model ss7's takeover barrier (ACQUIRE, reconcile every
execution host, retire superseded, re-drive pending, dispatch). `start_run` is its degenerate
case: an empty log has nothing to replay, reconcile or re-drive, so only the acquire remains.
"""

from __future__ import annotations

import contextlib
import os

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from dsl41 import runner_procid as _procid
from dsl41.ir import CatalogIR, JobIR
from dsl41.oracle_state import LIVE, Event, TERMINAL
from dsl41.runner import Engine, adapter_switch
from dsl41.runner_adapters import (
    JobAdapter,
    fsync_dir,
    Terminated,
    load_json,
    outcome_from_status,
    read_watch_log,
    resolve_spool,
    status_payload,
)
from dsl41.runner_clock import Clock, EngineError
from dsl41.runner_effects import Effect, EffectOutcome, Outbox
from dsl41.state_machine import Executor
from dsl41.run_root import (
    GENESIS_SEGMENT_NO,
    require_period_one,
    Manifest,
    RuntimeProfile,
    StagedManifest,
    claim_root,
    disagreements,
    estate_wal,
    check_manifest_against_segment,
    genesis_manifest,
    read_period_manifest,
    open_wal,
    wal_path,
    write_period_manifest,
    split_run_dir,
    switches_of,
    tz_aliases_of,
)
from dsl41.runner_journal import (
    check_segment_identity,
    Journal,
    RecoveredAttempt,
    ReplayFault,
    last_journal_at,
    baseline_id,
    read_journal,
    scheduler_frontier,
    replay_inputs,
)
from dsl41.runner_ledger import (
    STATE_MACHINE_VERSION,
    Fence,
    LeaderLock,
    acquire_run_root,
    check_leader_eligibility,
    next_epoch,
)
from dsl41.runner_scheduler import Scheduler
from dsl41.semantics import ADAPTER_SWITCHES, SCHEDULER_SWITCHES, check_overrides


#: Profile fields no wired object can report: they act in preflight or over the catalog, never on
#: an adapter or a scheduler. They inherit the pin unless the launcher declares them
#: (`_derive_runtime_profile`). `semantics` is wired: the scheduler reports the switches it
#: compiles under and a wired adapter the switches it reads; the rest are declared or inherited
#: the same way (DL-252, DL-258, DL-259, DL-260).
_UNWIRED_FIELDS: tuple[str, ...] = ("as_machine", "machine_policy")


def _derive_runtime_profile(
    scheduler: Scheduler | None,
    adapters: Mapping[str, JobAdapter],
    deadman_s: float | None,
    base: RuntimeProfile | None,
    declared: RuntimeProfile | None = None,
) -> RuntimeProfile:
    """The runtime profile the engine is actually wired with (period-model ss2.1, DL-130).

    The pin describes the machine that runs, not the flags somebody typed: an embedder that builds
    a Europe/Zurich scheduler and stages nothing would otherwise open a period whose hash says UTC
    while every tick is shifted. So the wired components are read back (the scheduler's timezone,
    the adapters' modes and windows, the deadman) over `base` for the fields the engine cannot
    see.

    `declared` is what the launcher was invoked with, and supplies exactly `_UNWIRED_FIELDS`.
    Without it those fields inherit the pin and cannot disagree with it, so a boundary that
    staged a new machine identity would open under the old one while this process answers to the
    names it started with (DL-151). A caller that declares nothing keeps the inheritance.

    The semantic switches start from `declared`, else `base` (DL-252). The ones a wired component
    acts on are read back over that: the scheduler's compiled switches, its calendar switches
    (DL-259) and `dst-start-times` (DL-260), and a wired adapter's, `fw-existence` from a
    `FileWatcherAdapter` (DL-258, DL-280). Wiring that disagrees with its declaration or the pin
    is refused by the drift gate."""
    from dsl41.run_root import SPAWN_WINDOW_S, RuntimeProfile, to_us
    from dsl41.runner_adapters import FileWatcherAdapter, LocalCommandAdapter

    values: dict[str, object] = dict((base or RuntimeProfile()).model_dump())
    if declared is not None:
        for name in _UNWIRED_FIELDS:
            values[name] = getattr(declared, name)
    if scheduler is not None:
        values["default_tz"] = scheduler.default_tz or "UTC"
        values["tz_aliases"] = dict(scheduler.tz_aliases)
    # the semantic switches: declared or inherited like the machine identity (DL-252), with the
    # scheduler's compiled switches read back like its timezone (DL-259, DL-260)
    switches = dict((declared or base or RuntimeProfile()).semantics)
    if scheduler is not None:
        for name in SCHEDULER_SWITCHES:
            switches[name] = scheduler.semantics.value(name)
    values["deadman_us"] = None if deadman_s is None else to_us(deadman_s)
    cmd = adapters.get("CMD")
    if isinstance(cmd, LocalCommandAdapter):
        values["execution_mode"] = "tethered"
        values["cmd_grace_us"] = to_us(cmd.grace_seconds)
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
    # the spawn window is a module constant, not an adapter knob: derive it from the value the
    # machine runs, so a staged 0 cannot pin a fiction over the real five seconds
    values["spawn_window_us"] = to_us(SPAWN_WINDOW_S)
    return RuntimeProfile.model_validate(values)


@dataclass
class Wiring:
    """The components a `RuntimeProfile` says an incarnation runs with.

    A profile, not a set of flags, because a run root is identified by its profile (ss2.1): a
    resume reads the manifest's, and a process that wired from its own defaults would refuse the
    root it was asked to continue."""

    adapters: dict[str, JobAdapter]
    scheduler: Scheduler
    #: the process's statechart executor (statechart-engine ss11); the caller hands the same one
    #: to `start_run` or `resume_run`, so a process with an engine has one executor
    machines: Executor = field(default_factory=Executor)


def wire_from_profile(catalog: CatalogIR, profile: RuntimeProfile, *, start: datetime) -> Wiring:
    """Build the adapters and the scheduler from `profile`, with the scheduler started at
    `start`.

    `start` is the caller's (DL-145). This function reads no clock: a builder that took `now()`
    could put a scheduler on a different instant from the engine it is wired into.

    The inverse of `_derive_runtime_profile`, and the one place a real run's components are
    constructed (DL-137): `dsl41 run` and its resume both come through here, so a window that
    moves in the profile moves in the components. It does not make the profile agree with the
    root's pin: `run` passes the profile the launcher built and `_manifest_profile_drift` on the
    resume ladder holds it to the pin."""
    from dsl41.runner_adapters import FileWatcherAdapter, LocalCommandAdapter

    if profile.execution_mode != "tethered":
        raise EngineError(
            f"execution mode {profile.execution_mode!r}: detached mode is retired in 2.0.0"
        )
    adapters = {
        "CMD": LocalCommandAdapter(grace_seconds=profile.cmd_grace_us / 1_000_000),
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
        # the extended calendars and the start instants compile under the run's switches, the
        # ones its engine runs (DL-259, DL-260)
        semantics=switches_of(profile),
    )
    return Wiring(adapters=cast("dict[str, JobAdapter]", adapters), scheduler=scheduler)


def _require_scheduler(catalog: CatalogIR, scheduler: Scheduler | None, where: str) -> None:
    """A catalog that schedules jobs needs a scheduler wired, and the right one (DL-130). The
    profile cannot see a scheduler's absence (default_tz inherits the pin and reports no drift),
    and one built over a different catalog carries another estate's plans with a matching
    timezone; either way scheduled execution stops or fires wrong. Run at genesis and at
    resume."""
    if scheduler is not None:
        # every supplied scheduler is held to its compile-time hash, before the scheduled-jobs
        # check: a catalog whose last trigger is removed after the compile would otherwise skip
        # the comparison while the stale plans fire the removed jobs
        from dsl41.run_root import catalog_hash_v2

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
    """Every executable job type must have an adapter wired (DL-130). The profile inherits pinned
    values for wiring it cannot see, so a missing adapter drifts nothing, and a job of that type
    reaches RUNNING with no process behind it (`plan_effects` suppresses the SPAWN for a type
    with no dispatch row). Run at genesis and at resume."""
    required = sorted(
        {job.job_type for job in catalog.jobs.values() if job.job_type != "BOX"} - set(adapters)
    )
    if required:
        raise EngineError(
            f"this catalog runs job type(s) {', '.join(required)} and the {where}"
            " wired no adapter for them -- their jobs would run with no process"
            " behind them (period-model ss2.1)"
        )


def profile_drift(derived: RuntimeProfile, pinned: RuntimeProfile) -> list[str]:
    """Which profile fields moved, by name; the caller reports the drift, not the values. The walk
    is `period.disagreements` (DL-137), shared by every artifact comparison, the core's gates
    here and the CLI's (`cli_run._profile_mismatch`); each keeps its own refusal sentence
    (DL-152)."""
    return sorted(name for name, _, _ in disagreements(derived, pinned, type(derived).model_fields))


def _manifest_profile_drift(
    manifest: Manifest,
    scheduler: Scheduler | None,
    adapters: Mapping[str, JobAdapter],
    deadman_s: float | None,
    declared: RuntimeProfile | None,
) -> list[str]:
    """Which profile fields this wiring moves off `manifest`'s pin: the one runtime gate against
    an installed manifest (DL-152), used by the resume ladder and the commit-a-staged-boundary
    path. Their refusals stay separate; each names why its period may not move, which tells an
    operator which launch to fix.

    The staged-manifest gate in `start_run` is not this: it compares against a manifest that is
    not installed yet."""
    derived = _derive_runtime_profile(
        scheduler, adapters, deadman_s, base=manifest.runtime_profile, declared=declared
    )
    return profile_drift(derived, manifest.runtime_profile)


def start_run(
    catalog: CatalogIR,
    run_root: Path,
    *,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    scheduler: Scheduler | None = None,
    hold_open: bool = False,
    deadman_s: float | None = None,
    lock: LeaderLock | None = None,
    staged: StagedManifest | None = None,
    launched: RuntimeProfile | None = None,
    machines: Executor | None = None,
) -> Engine:
    """ss1.1's genesis transaction, in its order, plus an Engine wired to what it created.

    Four ordered steps; the order is the recovery argument:

    1. `flock` `leader.lock`, before the refusal: the refusal reads the root's state, and one
       leader per run root is the rule under which that read is meaningful (DL-99);
    2. the `period_root` sentinel, create-only under ss1.1's ownership rule;
    3. the bundle and `periods/000001/manifest.json`;
    4. `wal/000001.jsonl` with its `segment` record.

    A crash after step 2 and before step 4 leaves a root a re-run of genesis completes, reading
    `estate_id` back from the sentinel (PR-01a). Once a segment exists, ordinary `--resume` owns
    recovery and this refuses; there is no silent re-baselining.

    The manifest is installed before the log opens, and both derive from one object (PR-22).
    `staged` is what the launcher pinned: catalog hash, bundle address, runtime profile. A caller
    with none gets `launched` (the launch options, the default profile when absent) over the
    empty bundle, with the wired fields read back; the semantic switches follow the launcher
    (DL-259). A failure here releases the lock. `machines` is the process's statechart executor,
    the wiring's; with none the engine makes its own."""
    # the root is created by acquire_run_root (mkdir_durable fsyncs every new entry), not
    # pre-created here: a plain mkdir first would make the helper see an existing directory and
    # prove nothing about its dirent
    lock = lock or acquire_run_root(run_root)
    try:
        # the run root holds the journal (global values, every control input), job output, and
        # data: owner-only by explicit mode, not by umask
        os.chmod(run_root, 0o700)
        root = claim_root(run_root)  # step 2
        if wal_path(run_root, GENESIS_SEGMENT_NO).exists():
            raise EngineError(
                f"{wal_path(run_root, GENESIS_SEGMENT_NO)} already exists: resume it"
                " (resume_run) or pick a fresh run root"
            )
        return _finish_genesis(
            catalog,
            run_root,
            lock,
            Fence(lock),
            root_estate_id=root.estate_id,
            clock=clock,
            adapters=adapters,
            scheduler=scheduler,
            hold_open=hold_open,
            deadman_s=deadman_s,
            staged=staged,
            launched=launched,
            machines=machines,
        )
    except BaseException:
        # a refused genesis holds nothing: the raw-fd lock conflicts with a retry in this
        # process, so holding it would wedge every embedder and test that retries
        lock.release()
        raise


def _finish_genesis(
    catalog: CatalogIR,
    run_root: Path,
    lock: LeaderLock,
    fence: Fence,
    *,
    root_estate_id: str,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    scheduler: Scheduler | None,
    hold_open: bool,
    deadman_s: float | None,
    staged: StagedManifest | None,
    launched: RuntimeProfile | None,
    machines: Executor | None,
) -> Engine:
    """ss1.1 steps 3-4 plus the Engine, split out so `start_run` pairs the acquire with a release
    on every failure path. The engine runs on `machines`, the process's executor, when the caller
    has one."""
    (run_root / "runs").mkdir(exist_ok=True)
    (run_root / "logs").mkdir(exist_ok=True)
    at = clock.now()
    _require_adapters(catalog, adapters, "genesis")
    _require_scheduler(catalog, scheduler, "genesis")
    derived = _derive_runtime_profile(
        scheduler, adapters, deadman_s, base=staged.runtime_profile if staged else launched
    )
    if staged is not None:
        if staged.state_machine_version != STATE_MACHINE_VERSION:
            # one executable implements one state-machine version (period-model ss2.1):
            # committing a different pin would leave this engine beneath a manifest it can never
            # satisfy
            raise EngineError(
                f"staged state_machine_version {staged.state_machine_version}: this"
                f" build runs {STATE_MACHINE_VERSION}"
            )
        drift = profile_drift(derived, staged.runtime_profile)
        if drift:
            # the pin must describe the machine that runs: a staged profile the wiring disagrees
            # with is refused before it is made durable
            raise EngineError(
                f"staged runtime profile disagrees with the engine's wiring on"
                f" {', '.join(drift)} (period-model ss2.1)"
            )
    else:
        from dsl41.run_root import EMPTY_BUNDLE_HASH, stage_manifest

        staged = stage_manifest(
            catalog,
            source_bundle_hash=EMPTY_BUNDLE_HASH,
            profile=derived,
            state_machine_version=STATE_MACHINE_VERSION,
        )
    manifest = genesis_manifest(
        catalog,
        clock_domain="virtual" if clock.virtual else "real",
        state_machine_version=STATE_MACHINE_VERSION,
        staged=staged,
    )
    write_period_manifest(run_root, manifest)  # step 3
    journal = Journal.create(  # step 4
        open_wal(run_root, GENESIS_SEGMENT_NO),
        catalog=catalog,
        clock_domain=manifest.clock_domain,
        started_at=at,
        lock=fence,
        manifest=manifest,
        estate_id=root_estate_id,
    )
    epoch = next_epoch([])  # the first term over a log that has none
    try:
        journal.leader(epoch=epoch, at=at)
        lock.note(epoch=epoch, at=at)
        fsync_dir(journal.path.parent)  # the segment's directory entry is a record too
        fsync_dir(run_root)
    except BaseException:
        # no engine will own the segment: close it, as a refused genesis releases the lock (a
        # statechart error stops here too, DL-336). A failing close never masks the original error
        with contextlib.suppress(OSError, ValueError):
            journal.close()
        raise
    return Engine(
        catalog,
        clock=clock,
        adapters=adapters,
        journal=journal,
        run_root=run_root,
        scheduler=scheduler,
        hold_open=hold_open,
        deadman_s=deadman_s,
        epoch=epoch,
        manifest=manifest,
        fence=fence,
        semantics=switches_of(manifest.runtime_profile),
        machines=machines,
    )


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
    deadman_s: float | None = None,
    lock: LeaderLock | None = None,
    declared: RuntimeProfile | None = None,
    machines: Executor | None = None,
) -> Engine:
    """ss7 resume: hash-gate, replay, reconcile. Returns an Engine with the reconciliation
    completions queued (source=reconcile); the caller runs the loop to process them and continue
    the run.

    A `scheduler` is re-anchored at the last journal instant inclusive and deduped against the
    journal's own scheduler ticks (a crash between same-instant siblings' appends must lose none
    silently); the unjournaled remainder of the window up to wall-now was missed across downtime
    and is dropped and journaled, reported on Engine.drops, never fired late (PENDING: E9; a
    live-but-stalled engine fires its backlog, downtime never does).

    `machines` is the process's statechart executor, the wiring's; the engine runs on it. With none the engine makes its own, as
    `start_run`'s does."""
    # acquire first (S6a, concurrency-model ss7): everything below reads or acts (the log is
    # replayed, the estate reconciled, recorded kills re-driven), and a mutex taken after the
    # first side effect is not a mutex. It also precedes the read, or another engine could append
    # between the read and the acquire and this one would allocate an epoch the log already used.
    lock = lock or acquire_run_root(run_root)
    #: the WAL the ladder opened, closed again if it refuses
    journals: list[Journal] = []
    try:
        engine = await _resume_under_lock(
            catalog,
            run_root,
            lock,
            journals=journals,
            clock=clock,
            adapters=adapters,
            scheduler=scheduler,
            hold_open=hold_open,
            settle_seconds=settle_seconds,
            grace_seconds=grace_seconds,
            deadman_s=deadman_s,
            declared=declared,
            machines=machines,
        )
    except BaseException:
        # a refused resume holds nothing, so the next engine may lead; the WAL it opened is
        # closed too, and the lock is released below
        for journal in journals:
            # never mask the refusal with a second error: a WAL already closed, or a flush that
            # fails on the way out
            with contextlib.suppress(OSError, ValueError):
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
    deadman_s: float | None,
    declared: RuntimeProfile | None = None,
    journals: list[Journal],
    machines: Executor | None = None,
) -> Engine:
    """The ss7 resume ladder proper, with leadership already held (S6a). Split from `resume_run`
    so the acquire/release pairing is one block."""
    fence = Fence(lock)
    require_period_one(run_root)  # a second segment refuses before it is read
    wal = estate_wal(run_root)
    records = read_journal(wal)
    opening = records[0]
    # before any replay or write: the WAL is this root's (its sentinel's estate, its own
    # filename) and holds period 1 only
    check_segment_identity(wal, opening)
    manifest = _pinned_manifest(
        run_root, opening, catalog, scheduler, adapters, deadman_s, declared
    )
    settle_seconds, grace_seconds = _reconcile_windows(manifest, settle_seconds, grace_seconds)
    # unconditional (DL-137): the guard runs at genesis and at resume, and a manifest-less root
    # never reaches this line
    _require_adapters(catalog, adapters, "resume")
    _require_scheduler(catalog, scheduler, "resume")
    last_at = _check_resume_clock(opening, records, clock)
    # tighten a pre-existing looser root (same reason as create), only once every check that can
    # refuse has passed, so a refused root is left as it was
    os.chmod(run_root, 0o700)
    journal = Journal(
        estate_wal(run_root),
        fsync_each=not clock.virtual,
        baseline_id=baseline_id(records),
        lock=fence,
    )
    journals.append(journal)
    # the term is allocated by being appended (ss1), before the first input this incarnation
    # admits, so every record after it names its author
    epoch = next_epoch(records)
    journal.leader(epoch=epoch, at=clock.now())
    lock.note(epoch=epoch, at=clock.now())
    engine = Engine(
        catalog,
        clock=clock,
        adapters=adapters,
        journal=journal,
        run_root=run_root,
        scheduler=scheduler,
        hold_open=hold_open,
        deadman_s=deadman_s,
        epoch=epoch,
        manifest=manifest,
        fence=fence,
        # the pin, so the oracle reads what replay reads (DL-252); the engine checks it against
        # the manifest
        semantics=switches_of(manifest.runtime_profile),
        machines=machines,
    )
    evidence = await _replay_into_engine(engine, records, run_root)
    # seed the ghost-run gate: replayed starts are reconciliation's business, not a fresh
    # dispatch
    for job, rt in engine.oracle.store.job.items():
        if rt.run_number:
            engine._dispatched[job] = rt.run_number
    if scheduler is not None:
        # the ticks this segment's journal has already adjudicated: admitted as a scheduler
        # `input` (`Engine._enqueue` -> `Journal.admit`, replay already fed them) or dropped and
        # journaled by an earlier resume's sweep below (`journal.drop`, the one call site). Each is
        # a durable verdict, which re-deriving must read and not act on again (DL-166, DL-174).
        # `advance` records (DL-44) carry neither `kind` nor a job and never hold a tick's fate,
        # so they stay out of this set; the `kind == "STARTJOB"` filter excludes them. It is
        # sound because `pop_due` is the only source of an admitted or dropped tick and yields
        # only STARTJOB; a non-STARTJOB drop would need the filter widened.
        replayed_ticks = {
            (record["payload"].get("job"), record["at"])
            for record in records
            if record.get("kind") == "STARTJOB"
            and (
                (record.get("rec") == "input" and record.get("source") == "scheduler")
                or record.get("rec") == "drop"
            )
        }
        frontier = scheduler_frontier(records)
        # always inclusive of the frontier: with several jobs at one instant, a crash between the
        # siblings' input appends leaves the frontier at the tick with a sibling unjournaled, and
        # an exclusive anchor would lose it with no record (DL-45)
        scheduler.reset(frontier)
        sweep_upto = max(clock.now(), frontier)  # virtual resume: now < the frontier
        for tick_ev in scheduler.pop_due(sweep_upto):
            # this segment adjudicated the tick: admitted it, or an earlier resume dropped it. The
            # `drop` half keeps a drop-set frontier from being re-dropped on every later resume,
            # a durable duplicate (DL-166, DL-174). What it does not claim is dropped and
            # journaled once, never fired late.
            if (tick_ev.job(), tick_ev.at.isoformat()) in replayed_ticks:
                continue
            reason = "scheduler tick missed while the engine was down; not fired late"
            engine.drops.append((tick_ev, reason))  # PENDING: E9
            journal.drop(tick_ev, reason)
    await _reconcile(
        engine,
        records,
        last_at,
        settle_seconds=settle_seconds,
        grace_seconds=grace_seconds,
        evidence=evidence,
    )
    return engine


def _pinned_manifest(
    run_root: Path,
    opening: dict[str, Any],
    catalog: CatalogIR,
    scheduler: Scheduler | None,
    adapters: Mapping[str, JobAdapter],
    deadman_s: float | None,
    declared: RuntimeProfile | None,
) -> Manifest:
    """The segment's committed manifest, once the leader-eligibility, segment and runtime-profile
    gates have passed over it."""
    check_leader_eligibility(opening, catalog=catalog)
    # PR-22's U4 half: the committed manifest is the engine's own output, and one that is not
    # this segment's refuses. A missing artifact degrades and a wrong one refuses (DL-113
    # decision 5), but not here: genesis installs the manifest before the log opens, so a root
    # without one lost it, and degrading would skip every profile gate below. `read_journal`
    # refuses a `header` root in `_select_lineage` (DL-138). It has run ss2.1's schema over the
    # opening, so `period_id` is an int and a fallback would be a second authority (DL-138)
    manifest = read_period_manifest(run_root, int(opening["period_id"]))
    if manifest is None:
        raise EngineError(
            f"{run_root}: a segment journal with no periods/000001/manifest.json --"
            " the period's pin is missing (period-model ss2.1)"
        )
    check_manifest_against_segment(manifest, opening)
    # the runtime half of the same gate, in core: the wired components must be what the period
    # pinned, or shifted ticks and different kill windows run under an unchanged hash. Fields no
    # wiring can report inherit the pin unless the launcher declared them (`declared`): a process
    # that says nothing about its machine identity is held to the rest, and one that does is held
    # to all of it.
    drift = _manifest_profile_drift(manifest, scheduler, adapters, deadman_s, declared)
    if drift:
        raise EngineError(
            f"runtime-profile mismatch on {', '.join(drift)}: a runtime-profile"
            " change is a new period (period-model ss2.1); re-baseline"
            " explicitly with a fresh run root"
        )
    return manifest


def _reconcile_windows(
    manifest: Manifest, settle_seconds: float | None, grace_seconds: float | None
) -> tuple[float, float]:
    """The settle and grace windows: the caller's, else the pin's."""
    # the reconciliation windows have no wire flag, so the pin is their default: a None param
    # resolves from the manifest, and an explicit one is a caller's deliberate override (a
    # harness affordance, like wiring a different adapter object)
    if settle_seconds is None:
        settle_seconds = manifest.runtime_profile.reconcile_settle_us / 1_000_000
    if grace_seconds is None:
        grace_seconds = manifest.runtime_profile.cmd_grace_us / 1_000_000
    return settle_seconds, grace_seconds


def _check_resume_clock(
    opening: Mapping[str, Any], records: list[dict[str, Any]], clock: Clock
) -> datetime:
    """Refuse a resume clock of the wrong domain or behind the journal; answer the journal's last
    instant."""
    domain = "virtual" if clock.virtual else "real"
    if opening.get("clock_domain") != domain:
        raise EngineError(
            f"clock-domain mismatch: journal is {opening.get('clock_domain')!r},"
            f" resume clock is {domain!r}"
        )
    last_at = last_journal_at(records)
    if not clock.virtual and last_at > clock.now():
        raise EngineError(
            f"journal is from the future ({last_at.isoformat()} > now): the machine"
            " clock moved backwards; refusing to feed non-decreasing time backwards"
        )
    return last_at


#: What the ss7 sweep reconciles against: each run's spool directory, keyed by (job, run number).
type ResumeEvidence = dict[tuple[str, int], Path | None]


async def _replay_into_engine(
    engine: Engine,
    records: list[dict[str, Any]],
    run_root: Path,
) -> ResumeEvidence:
    """Replay the segment into `engine`, installing the log's position (frontiers and decisions)
    and the outbox on it, and answer the evidence the recovered attempts leave."""
    # the rebuilt outbox's effects and the decision index's request ids in flight run on the
    # engine's executor, the process's one (statechart-engine ss11)
    try:
        replay = replay_inputs(
            engine.oracle,
            records,
            outbox=Outbox(engine.machines),
            machines=engine.machines,
        )
    except ReplayFault as exc:
        # refuse, don't degrade. The same build raises at the same input on
        # every resume, so the estate stays down until a build that replays it runs here
        # (deployment runbook ss7)
        raise EngineError(
            f"{run_root}: resume stopped: {exc}. Deploy a build that fixes the"
            " fault, or roll back to the release that wrote this log",
            code=exc.code,
        ) from exc
    # the log's position comes back with its contents (concurrency-model ss2): the next admission
    # continues the index, and a retry of anything this log decided is answered from that
    # decision, not applied again
    engine.frontiers = replay.frontiers
    engine.decisions = replay.decisions
    # ss5: the effects the previous engine intended, and what became of them. An engine that
    # forgot a kill it had decided would leave a detached run orphaned: its job is already
    # TERMINAL, so reconciliation skips it and nothing else would look again.
    engine.outbox = replay.outbox
    evidence = await _commit_recovered_after_identity_check(
        engine, replay.recovered_attempts, records
    )
    return evidence


async def _commit_recovered_after_identity_check(
    engine: Engine,
    recovered: list[RecoveredAttempt],
    records: list[dict[str, Any]],
) -> ResumeEvidence:
    """ss4 step 7 for each attempt the previous engine admitted and never decided (DL-315): plan,
    check, then write.

    Every recovered attempt is planned first; with no live run a plan holds SPAWNs only, so no
    plan reads another's. What the spool and the host hold is then read and checked against the
    WAL's SPAWNs and the planned ones (DL-118). Only then are the decisions written, so a refused
    resume has written no decision and minted no id, and the build that wrote the log can still
    open it. The decisions land before anything after this reads the outbox: a recovered start is
    then a pending SPAWN like any other, which the barrier's dispatch applies or a held host
    holds. Returns the evidence read, for the reconciliation ladder."""
    planned = [(attempt, engine.plan_recovered(attempt)) for attempt in recovered]
    candidates = await _resume_evidence(engine, records)
    _preflight_identities(
        engine,
        candidates,
        planned=[effect for _, effects in planned for effect in effects],
    )
    for attempt, effects in planned:
        engine.commit_recovered(attempt, effects)
    return candidates


async def _resume_evidence(engine: Engine, records: list[dict[str, Any]]) -> ResumeEvidence:
    """The runs the ss7 sweep reconciles. Read only: nothing here appends or launches."""
    assert engine.run_root is not None
    # sweep = union(journal dispatch records, runs/ directory) (ss7)
    candidates: dict[tuple[str, int], Path | None] = {}
    for record in records:
        if record.get("rec") == "dispatch":
            run_dir = record.get("run_dir")
            candidates[(record["job"], int(record["run_number"]))] = (
                Path(run_dir) if run_dir else None
            )
    runs_dir = engine.run_root / "runs"
    if runs_dir.is_dir():
        for entry in sorted(runs_dir.iterdir()):
            run = split_run_dir(entry.name)
            if entry.is_dir() and run is not None:
                # the canonical parser (DL-137): an inline rpartition accepts `b.01` as run 1, and
                # setdefault over the sorted listing would let a directory this estate never wrote
                # answer the ss7 ladder for a real run's fate
                candidates.setdefault(run, entry)
    return candidates


async def _reconcile(
    engine: Engine,
    records: list[dict[str, Any]],
    last_at: datetime,
    *,
    settle_seconds: float,
    grace_seconds: float,
    evidence: ResumeEvidence | None = None,
) -> None:
    """The ss6a/ss7 reconciliation ladder (module docstring). Tethered semantics did the killing
    already (wrappers EOF'd when the engine died), so this mostly reads; signals are for the
    residual crash matrix, and only at a (pid, start-time)-verified target.

    `evidence` is `_resume_evidence`'s reading when the caller has taken it: resume reads it and
    checks it against the WAL and the planned SPAWNs before it writes the recovered decisions.
    The preflight below re-checks it against the outbox as it now stands."""
    assert engine.run_root is not None
    boot_now = _procid.current_boot_id()
    candidates = evidence if evidence is not None else await _resume_evidence(engine, records)
    _preflight_identities(engine, candidates)
    _reconcile_applied_spawns(engine, candidates)

    for (job, run_number), run_dir in sorted(candidates.items()):
        rt = engine.oracle.store.job.get(job)
        if rt is None or rt.run_number != run_number or rt.status in TERMINAL:
            continue  # superseded run, or its completion already replayed
        job_ir = engine.oracle.catalog.jobs.get(job)
        if job_ir is None:
            continue
        # DL-242: only a live row is relaunched. A row an operator set INACTIVE, a SEM-18 cascade
        # or a box-start reset moved off its run at the same run number is DL-235's orphan: its
        # evidence is still reconciled below (the stale-completion gate rejects it), but nothing
        # starts the run again
        live = rt.status in LIVE
        if job_ir.job_type == "FW":
            _resume_watch(engine, job_ir, run_number, run_dir, last_at, relaunch=live)
            continue
        bound = _spawn_effect_for(engine, job, run_number)
        # the ladder resolves this run's fate from its directory; the preflight checked the
        # directory's claim to be this run's, and the bound id rides along so a record that
        # appears between the preflight and this read is held to the same identity
        result, ended_at = await resolve_spool(
            job,
            run_number,
            run_dir,
            boot_now,
            settle_seconds=settle_seconds,
            grace_seconds=grace_seconds,
            expected_run_id=bound.run_id if bound is not None else None,
        )
        # the owner's reading (DL-145): a value that is not an int or a `Terminated` is refused,
        # as on the live path, and does not become a fabricated fate at resume
        extras = status_payload(result, where=f"the spool ladder for {job}.{run_number}")
        if ended_at is not None:
            extras["ended_at"] = ended_at.isoformat()  # true end time (ss7)
        _inject_completion(engine, job, run_number, extras, at=ended_at or last_at, last_at=last_at)

    _resume_untraced_starts(engine, candidates, last_at)
    await _redrive_recorded_kills(engine)
    # ss7's barrier ends where it says: ACQUIRE -> reconcile -> retire superseded, re-drive
    # pending -> DISPATCH. Otherwise the outbox drains only after the next admitted input (the
    # loop dispatches on the way out of `_admit_and_apply`), so a re-driven start would wait on
    # unrelated traffic: hours on a quiet estate, forever where the only remaining work is the
    # run that was lost. Everything pending here is a SPAWN nothing applied: the kills above are
    # resolved, and a SPAWN whose run reached the host was reconciled from its trace.
    # Superseded ones retire on the way through, and a drained or quarantined host holds its
    # own, in the one gate that owns that call.
    engine._dispatch()


def _resume_watch(
    engine: Engine,
    job_ir: JobIR,
    run_number: int,
    run_dir: Path | None,
    last_at: datetime,
    *,
    relaunch: bool = True,
) -> None:
    """One incomplete FW run, from its spool (period-model ss2.2).

    A dispatched watch leaves a run directory, so a resumed watch lands here: the sweep finds the
    directory, and the log says whether the watch is over. `relaunch=False` is a row that is not
    live (DL-242): a completed log is still injected, for the gate to judge, and an incomplete
    one is left alone."""
    from dsl41.runner_adapters import FileWatcherAdapter
    from dsl41.semantics import fw_existence_immediate

    job = job_ir.name
    if run_dir is None and engine.run_root is not None:
        # the preflight's fallback: a candidate that came from a dispatch record without a
        # run_dir still has one, and without it the inject-from-log branch below is unreachable
        run_dir = engine.run_root / "runs" / f"{job}.{run_number}"
    watch = read_watch_log(run_dir) if run_dir is not None else None
    bound = _spawn_effect_for(engine, job, run_number)
    if watch is not None and bound is not None:
        # re-checked at this read, not only at the preflight: a log that appeared in the window
        # between them is held to the same identity, or its fate would be injected as this run's
        _refuse_identity_split(bound, watch.run_id, "the spool's watch.jsonl")
    adapter = engine.adapters.get("FW")
    if adapter is None:
        # _require_adapters runs at genesis and at resume, before reconciliation, and refuses a
        # catalog that runs an FW job with no FW adapter wired. This function is reached only for
        # job_ir.job_type == "FW", so that adapter is present.
        raise AssertionError(
            "_require_adapters already refuses a resume with an FW job and no FW"
            " adapter wired, so adapter cannot be None here"
        )
    # DL-258: an `immediate` watch with no minimum size is complete when the run's first poll
    # qualifies, not at FW_STABLE_POLLS, and only the first: `watch_seq == 2` is exactly one poll
    # line (the start line plus that one), so a later qualifying poll gets no carve-out. This is
    # `FileWatcherAdapter.run`'s live reading, so a crash between that first poll and the live
    # return resolves the same way.
    immediate = isinstance(adapter, FileWatcherAdapter) and fw_existence_immediate(
        job_ir, adapter.existence
    )
    immediate_first_poll = immediate and watch is not None and watch.watch_seq == 2
    if watch is not None and (watch.complete or (immediate_first_poll and watch.qualifying)):
        # PR-34a: the last durable line is a completing observation and the row is still RUNNING:
        # the engine died between the poll and the STATUS input. Inject the completion from the
        # log, as a CMD's is injected from status.json; re-polling would decide the watch again
        # against a world that has moved on.
        extras: dict[str, object] = {"exit_code": 0}
        if watch.last_at is None:
            # a complete watch (WatchLog.complete, stable_polls >= FW_STABLE_POLLS) has at least
            # one poll line, and an immediate completion (`watch.qualifying`) defaults False with
            # no poll line (read_watch_log); either way last_at, set from that line, is not None
            raise AssertionError(
                "a complete or immediately-qualifying watch has at least one poll"
                " line, so last_at cannot be None (WatchLog invariant)"
            )
        extras["ended_at"] = watch.last_at.isoformat()
        _inject_completion(engine, job, run_number, extras, at=watch.last_at, last_at=last_at)
        return
    if not relaunch:
        return
    if bound is None or engine.outbox.state_of(bound.effect_id) == "retired":
        # the live engine planned no launch for this run, or retired the one it planned: the
        # sweep's rule for an untraced start holds for a traced one, and a watch is relaunched
        # only under the identity of a SPAWN that was applied (DL-315)
        return
    # idempotent read: the adapter reconstructs progress from the log and appends no second
    # `start` line. The bound id rides along for the case with no log to reconstruct from (a run
    # directory made, then a crash before the `start` line), where a watch dispatched with no
    # identity would write `run_id: null` and split from the WAL (DL-118).
    engine._launch(job_ir, run_number, adapter, run_id=bound.run_id)


def _inject_completion(
    engine: Engine,
    job: str,
    run_number: int,
    extras: dict[str, object],
    *,
    at: datetime,
    last_at: datetime,
) -> None:
    """One reconciliation verdict, as an input. `source="reconcile"` makes it a completion, and so
    subject to the ss4 stale gate: it is a verdict about a run the log may already know the end
    of."""
    engine._enqueue(
        Event(
            at=max(at, last_at),  # feed times are non-decreasing (ss7)
            kind="STATUS",
            payload={"job": job, "run_number": run_number, **extras},
        ),
        source="reconcile",
    )


def _resume_untraced_starts(
    engine: Engine, candidates: dict[tuple[str, int], Path | None], last_at: datetime
) -> None:
    """Starts with no trace anywhere: no run directory, no dispatch record, and nothing the host
    admits to running (S6c).

    This is where ss7's "re-drive pending" step meets a row. The ladder above asks
    how runs that left a trace ended; this asks what to do about a decision that left none. The
    answer is the state of the row's own SPAWN (DL-315). Resume has committed every attempt whose
    decision never landed, so a dispatchable row at run N reached it through a decision, and that
    decision's plan records what the live engine meant to do for run N."""
    for job, rt in engine.oracle.store.job.items():
        if rt.status not in LIVE or (job, rt.run_number) in candidates:
            continue
        job_ir = engine.oracle.catalog.jobs.get(job)
        if job_ir is None or job_ir.job_type == "BOX":
            continue  # boxes fold from members; pseudo-entries have no dispatch
        bound = _spawn_effect_for(engine, job, rt.run_number)
        state = engine.outbox.state_of(bound.effect_id) if bound is not None else None
        if state == "pending":
            # re-driven. The log holds this run's intent to spawn, never resolved, and nothing ran:
            # the previous leader died between recording what it meant to do and doing it. It stays
            # pending, which is all re-driving takes: `_dispatch` drains the outbox when the loop
            # runs, through the gates a fresh effect passes, so a drained or quarantined host
            # still holds it (ss8). An FW start too: its watch waits for the same SPAWN, so a held
            # watch stays held across the restart (DL-96, DL-102). Only this run's SPAWN counts: a
            # pending SPAWN of an earlier run says nothing about run N, and dispatch retires it.
            continue
        if bound is None or state == "retired":
            # left as the live engine left it. Run N's SPAWN was retired at dispatch (a retired
            # effect stays retired, DL-232), or the decision that put the row at run N planned none:
            # a STARTING overwrite at the same run number, or a row carried across a boundary whose
            # SPAWN the previous period retired. Either way the live engine launched nothing for
            # this run and never would, so neither does resume. The row stays live at run N with
            # nothing running or held, for the operator to end or restart. A tethered command is
            # not failed: nothing was lost (DL-315).
            continue
        # tried, and no trace. The SPAWN resolved `applied`: dispatch reached the adapter, and the
        # crash took whatever it left before anything durable named the run. `indeterminate` takes
        # the same ladder; the ladder lets the host decide where it can, which is what the state
        # means (concurrency-model ss5)
        if job_ir.job_type == "FW":
            # the watch is an idempotent re-read, so it is re-dispatched and not failed (DL-44
            # item 7, DL-102), under its bound id. The routing gate passed at the live dispatch,
            # and a running watch continues on a host that routes nothing new (ss8)
            adapter = engine.adapters.get("FW")
            if adapter is None:
                # _require_adapters runs at genesis and at resume, before reconciliation, and
                # refuses a catalog that runs an FW job with no FW adapter wired. This branch is
                # reached only for job_ir.job_type == "FW", so that adapter is present.
                raise AssertionError(
                    "_require_adapters already refuses a resume with an FW job and no"
                    " FW adapter wired, so adapter cannot be None here"
                )
            engine._launch(job_ir, rt.run_number, adapter, run_id=bound.run_id)
            continue
        # failed. A tethered wrapper died with the engine and left no trace. runner-design ss7 fails such a start rather
        # than silently re-run it. An indeterminate SPAWN may have run, so its cause is E7's, not
        # "never spawned"
        cause = (
            "exit_status_unobservable"  # PENDING: E7
            if state == "indeterminate"
            else "dispatch lost to engine crash (never spawned)"
        )
        _inject_completion(
            engine,
            job,
            rt.run_number,
            {"status": "FAILURE", "cause": cause},
            at=last_at,
            last_at=last_at,
        )


def _spawn_effect_for(engine: Engine, job: str, run_number: int) -> Effect | None:
    """The durable SPAWN that bound this run's identity, in any state, resolved included: the
    split to refuse is between the WAL and what a spool or a supervisor claims now, and
    resolution does not expire the binding."""
    return next(
        (
            e
            for e in engine.outbox.effects()
            if e.kind == "SPAWN" and (e.job, e.run_number) == (job, run_number)
        ),
        None,
    )


def _refuse_identity_split(effect: Effect, observed: object, source: str) -> None:
    """One key runs through the WAL and the spool (DL-118, PR-36a): the durable effect bound this
    run's `run_id` at birth, so present evidence about the run must name that id. A different id,
    or none where a new-writer wrapper always records one, is a WAL/spool split: corruption, a
    spoofed record, or a directory from another estate, and reattaching to, resolving, or killing
    that process would act on a run the log never spawned. It refuses loudly; there is no correct
    pick between two identities for one run. Callers pass only evidence that exists: an absent
    file is the ladder's business. A pre-DL-118 effect has no bound id and checks nothing."""
    if effect.run_id is None:
        return
    observed_id = str(observed) if observed is not None else None
    if observed_id != effect.run_id:
        raise EngineError(
            f"{effect.job}.{effect.run_number}: {source} reports run_id"
            f" {observed_id!r} but the durable effect bound {effect.run_id!r}"
            " -- refusing to act on a run the log did not spawn (DL-118)"
        )


def _preflight_identities(
    engine: Engine,
    candidates: dict[tuple[str, int], Path | None],
    *,
    planned: Sequence[Effect] = (),
) -> None:
    """Check every candidate's observed identities against the WAL before the barrier mutates
    anything (DL-118). One sweep up front: branch-by-branch checks leave ordering holes (a
    reconciliation that durably recorded `applied` before a later branch refused the same run
    leaves the refusal half-taken).

    `planned` holds the SPAWNs of the recovered decisions resume is about to write (DL-315); a
    run's id is bound by its durable SPAWN or by one of these. Resume runs this first, before it
    writes the recovered decisions, so a refusal there has written no decision and launched
    nothing: the run root holds only this incarnation's `leader` record beyond what the crash
    left. The barrier runs it again over the same evidence once the decisions are written, and it
    passes unless the spool changed in between."""
    for (job, run_number), run_dir in sorted(candidates.items()):
        effect = _spawn_effect_for(engine, job, run_number) or next(
            (
                e
                for e in planned
                if e.kind == "SPAWN" and (e.job, e.run_number) == (job, run_number)
            ),
            None,
        )
        if effect is None or effect.run_id is None:
            continue
        directory = run_dir
        if directory is None:
            # a candidate comes from resume's sweep, which runs on a rooted engine (ss7)
            assert engine.run_root is not None
            directory = engine.run_root / "runs" / f"{job}.{run_number}"
        for name in ("spawn.json", "status.json"):
            doc = load_json(directory / name)
            if doc is not None:
                _refuse_identity_split(effect, doc.get("run_id"), f"the spool's {name}")
        # an FW watch spawns no process; its `start` line names the run and is present
        # evidence like any other
        watch = read_watch_log(directory)
        if watch is not None:
            _refuse_identity_split(effect, watch.run_id, "the spool's watch.jsonl")


def _reconcile_applied_spawns(
    engine: Engine, candidates: dict[tuple[str, int], Path | None]
) -> None:
    """Resolve the pending SPAWNs whose runs did reach the host (ss5).

    `_apply_spawn` launches and then records the outcome, so an engine that died between the two
    leaves a pending effect for a run that may have started. The spool is the record (DL-93): a
    run directory means it reached the host, so the effect is reconciled from it and not
    re-driven, or the next dispatch would drain a pending SPAWN into a second `mkdir()` of a
    directory that exists."""
    for effect in [e for e in engine.outbox.pending() if e.kind == "SPAWN"]:
        spool = candidates.get((effect.job, effect.run_number))
        if (effect.job, effect.run_number) not in candidates:
            continue
        spawned = load_json(spool / "spawn.json") if spool is not None else None
        if spawned is not None:
            # re-checked at this read, not only at the preflight (DL-118): evidence that appeared
            # between them is held to the bound identity before anything durable is recorded,
            # including present-but-idless evidence, which the strict gate refuses. Otherwise the
            # applied outcome lands first and the later gates refuse after the WAL has moved.
            _refuse_identity_split(effect, spawned.get("run_id"), "the spool's spawn.json")
        run_id = (spawned or {}).get("run_id")
        detail = "reconciled from the spool: the run reached the host"
        if spawned is None and spool is not None:
            # ss11's FW rule (PR-34): a watch writes no spawn.json, and its `start` line says the
            # dispatch happened. Otherwise the ladder treats the pending SPAWN as an applied-SPAWN
            # candidate, finds no spawn.json, and re-launches the watch as an untraced start: two
            # `start` lines and a fold nothing can reproduce.
            watch = read_watch_log(spool)
            if watch is not None:
                _refuse_identity_split(effect, watch.run_id, "the spool's watch.jsonl")
                run_id = watch.run_id
                detail = "reconciled from the watch log: the start line is the dispatch"
        engine._resolve_effect(
            EffectOutcome(
                effect_id=effect.effect_id,
                state="applied",
                run_id=run_id,
                detail=detail,
            )
        )


async def _redrive_recorded_kills(engine: Engine) -> None:
    """Deliver the kills the previous engine decided and did not get to (concurrency-model ss5;
    S5c).

    A tethered run dies with the engine, so a kill the previous engine decided and did not
    deliver is resolved from the spool. Re-driving is not a new licence: runner-design ss7
    permits exactly this side effect at resume ("no side effects on resume beyond recorded
    kills").

    A kill whose run is not alive is resolved from the spool, three ways (ss5's third state).
    `status.json` saying the command was signalled means the kill landed; saying it exited means
    it finished first and the kill is retired in favour of the truth. No status record and no
    live wrapper means nobody can say whether the signal landed, and `indeterminate` is the only
    honest answer: either other report would invent a fact about a process nothing observed (E7)."""
    assert engine.run_root is not None
    for effect in [e for e in engine.outbox.pending() if e.kind == "KILL"]:
        engine._resolve_effect(_kill_outcome_from_spool(engine.run_root, effect))


def _kill_outcome_from_spool(run_root: Path, effect: Effect) -> EffectOutcome:
    """What the spool can say about an undelivered kill (ss5's three states).

    The record is read through `outcome_from_status`, the mapping the live adapter path and
    reconciliation share. Its `Terminated` means "this run ended by a kill"; `observed` is the
    wrapper's forensics about how the group died, not its verdict."""
    run_dir = run_root / "runs" / f"{effect.job}.{effect.run_number}"
    status = load_json(run_dir / "status.json")
    if status is not None:
        _refuse_identity_split(effect, status.get("run_id"), "the spool's status.json")
    if status is None:
        return EffectOutcome(
            effect_id=effect.effect_id,
            state="indeterminate",
            detail="no status record and no live wrapper: nothing can say whether it landed",
        )
    killed = isinstance(outcome_from_status(status), Terminated)
    return EffectOutcome(
        effect_id=effect.effect_id,
        state="applied" if killed else "retired",
        run_id=status.get("run_id"),
        detail=(
            "the spool records the run as killed"
            if killed
            else "the spool records no kill: the run exited on its own, failed to"
            " spawn, or left a record this build does not recognise"
        ),
    )
