"""Taking possession of a run root (ss7): genesis and resume (DL-74, DL-106).

The Engine is the single-writer loop over a live estate. What lives here runs once per
incarnation, before that loop exists: it creates or claims a run root, takes its lock, replays
the log, reconciles the estate against the world, and hands back an Engine ready to run. The
two halves share exactly one object, the one this half constructs. The name avoids
"lifecycle", which DL-42 gives to the wrapper tier.

- Resume (ss7): refuse on catalog-hash or clock-domain mismatch, replay inputs through a fresh
  Oracle, seed the ghost-run gate so replayed starts never respawn, then reconcile from the
  spool ladder: live wrapper -> settle window; status.json -> inject the real completion at
  max(ended_at, last journal at) with the true ended_at in the payload; verified command group
  orphaned by a dead wrapper -> kill it, TERMINATED "wrapper lost; killed at resume" (a kill
  that happened); nothing -> FAILURE exit_status_unobservable (PENDING: E7). First, an attempt
  the previous engine admitted and never decided gets its decision record (DL-315). A live start
  with no run directory is then decided by the log (DL-102): a run with a `dispatch` record was
  launched and left no trace, so it is FAILURE "dispatch lost to engine crash", because a run
  provably never started is never silently re-executed; a start replay derives with no
  `dispatch` record was decided and never launched, and is dispatched now; anything else, such
  as a STARTING overwrite at a run that never started, is left as it is. FW watchers differ:
  polling is an idempotent read, so a dispatched watch with no trace is re-dispatched under its
  recorded `run_id`. Reconciliation completions go through the ss4 stale gate like any adapter
  completion: if replay already reached a terminal state (say a term_run_time TERMINATED), the
  late real record is dropped and journaled, never silently overwritten.

`start_run` is the degenerate case: an empty log has nothing to replay, reconcile or dispatch,
so only the acquire remains.
"""

from __future__ import annotations

import contextlib
import os

from collections.abc import Mapping
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
    load_json,
    read_watch_log,
    resolve_spool,
    status_payload,
)
from dsl41.runner_clock import Clock, EngineError
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
    dispatched_runs,
    Journal,
    Replay,
    ReplayFault,
    last_journal_at,
    baseline_id,
    read_journal,
    scheduler_frontier,
    replay_inputs,
)
from dsl41.runner_ledger import (
    STATE_MACHINE_VERSION,
    LeaderLock,
    acquire_run_root,
    check_leader_eligibility,
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
    reaches RUNNING with no process behind it (`dispatch_plan` plans no start for a type with no
    dispatch row). Run at genesis and at resume."""
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
       engine per run root is the rule under which that read is meaningful (DL-99);
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
        lock=lock,
        manifest=manifest,
        estate_id=root_estate_id,
    )
    try:
        lock.note(at=at)
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
        manifest=manifest,
        fence=lock,
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
    # acquire first (S6a, DL-99): everything below reads or acts (the log is replayed, the estate
    # reconciled, decided starts dispatched), and a mutex taken after the first side effect is not
    # a mutex. It also precedes the read, or another engine could append between the read and the
    # acquire.
    lock = lock or acquire_run_root(run_root)
    #: the WAL the ladder opened, closed again if it refuses
    journals: list[Journal] = []
    #: the engine the ladder built, whose tasks a refusal stops before the lock is released
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
            deadman_s=deadman_s,
            declared=declared,
            machines=machines,
        )
    except BaseException:
        # a refused resume holds nothing, so the next engine may lead: every task it started
        # (a relaunched watch) is cancelled and collected first, the WAL it opened is closed,
        # and the lock is released last
        for engine in engines:
            # never mask the refusal with the shutdown's own error
            with contextlib.suppress(Exception):
                await engine.shutdown()
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
    engines: list[Engine],
    machines: Executor | None = None,
) -> Engine:
    """The ss7 resume ladder proper, with the lock already held (S6a). Split from `resume_run` so
    the acquire/release pairing is one block."""
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
        lock=lock,
    )
    journals.append(journal)
    lock.note(at=clock.now())
    engine = Engine(
        catalog,
        clock=clock,
        adapters=adapters,
        journal=journal,
        run_root=run_root,
        scheduler=scheduler,
        hold_open=hold_open,
        manifest=manifest,
        fence=lock,
        # the pin, so the oracle reads what replay reads (DL-252); the engine checks it against
        # the manifest
        semantics=switches_of(manifest.runtime_profile),
        machines=machines,
    )
    engines.append(engine)
    replay = await _replay_into_engine(engine, records, run_root)
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
        spawns=replay.spawns,
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
type ResumeEvidence = dict[tuple[str, int], Path]


async def _replay_into_engine(
    engine: Engine,
    records: list[dict[str, Any]],
    run_root: Path,
) -> Replay:
    """Replay the segment into `engine`, install the log's position (frontiers and decisions) on
    it, and write the decision of every attempt the previous engine admitted and never decided
    (DL-315). Answers the replay, whose `spawns` the ladder reads."""
    # the decision index's request ids in flight run on the engine's executor, the process's one
    # (statechart-engine ss11)
    try:
        replay = replay_inputs(engine.oracle, records, machines=engine.machines)
    except ReplayFault as exc:
        # refuse, don't degrade. The same build raises at the same input on every resume, so the
        # estate stays down until a build that replays it runs here
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
    # the identity preflight runs first, so a refused resume has written no decision and the
    # build that wrote the log can still open it
    _preflight_identities(engine, _resume_evidence(engine), dispatched_runs(records), replay.spawns)
    for result in replay.recovered:
        engine.commit_recovered(result)
    return replay


def _resume_evidence(engine: Engine) -> ResumeEvidence:
    """The runs the ss7 sweep reconciles: every run directory under `runs/`. Read only: nothing
    here appends or launches. A run's `dispatch` record precedes its directory, so a dispatched
    run with no directory never reached its wrapper (`_resume_untraced_starts`)."""
    assert engine.run_root is not None
    candidates: ResumeEvidence = {}
    runs_dir = engine.run_root / "runs"
    if runs_dir.is_dir():
        for entry in sorted(runs_dir.iterdir()):
            run = split_run_dir(entry.name)
            if entry.is_dir() and run is not None:
                # the canonical parser (DL-137): an inline rpartition accepts `b.01` as run 1, and
                # a directory this estate never wrote would answer the ss7 ladder for a real
                # run's fate
                candidates.setdefault(run, entry)
    return candidates


async def _reconcile(
    engine: Engine,
    records: list[dict[str, Any]],
    last_at: datetime,
    *,
    settle_seconds: float,
    grace_seconds: float,
    spawns: frozenset[tuple[str, int]] | set[tuple[str, int]] = frozenset(),
) -> None:
    """The ss6a/ss7 reconciliation ladder (module docstring). Tethered semantics did the killing
    already (wrappers EOF'd when the engine died), so this mostly reads; signals are for the
    residual crash matrix, and only at a (pid, start-time)-verified target.

    `spawns` is replay's derived starts (`Replay.spawns`): a live row whose start is among them
    and has no `dispatch` record is dispatched now. The identity preflight runs again over the
    evidence as it now stands, so a record that appeared since resume's first read is held to
    the same identity."""
    assert engine.run_root is not None
    boot_now = _procid.current_boot_id()
    dispatched = dispatched_runs(records)
    candidates = _resume_evidence(engine)
    _preflight_identities(engine, candidates, dispatched, spawns)

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
        bound = dispatched.get((job, run_number))
        if job_ir.job_type == "FW":
            _resume_watch(engine, job_ir, run_number, run_dir, last_at, bound, relaunch=live)
            continue
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
            expected_run_id=bound,
        )
        # the owner's reading (DL-145): a value that is not an int or a `Terminated` is refused,
        # as on the live path, and does not become a fabricated fate at resume
        extras = status_payload(result, where=f"the spool ladder for {job}.{run_number}")
        if ended_at is not None:
            extras["ended_at"] = ended_at.isoformat()  # true end time (ss7)
        _inject_completion(engine, job, run_number, extras, at=ended_at or last_at, last_at=last_at)

    _resume_untraced_starts(engine, candidates, last_at, dispatched, spawns)


def _resume_watch(
    engine: Engine,
    job_ir: JobIR,
    run_number: int,
    run_dir: Path | None,
    last_at: datetime,
    bound: str | None,
    *,
    relaunch: bool = True,
) -> None:
    """One incomplete FW run, from its spool (period-model ss2.2).

    A dispatched watch leaves a run directory, so a resumed watch lands here: the sweep finds the
    directory, and the log says whether the watch is over. `bound` is the `run_id` the run's
    `dispatch` record names, None for a run this log never dispatched. `relaunch=False` is a row
    that is not live (DL-242): a completed log is still injected, for the gate to judge, and an
    incomplete one is left alone."""
    from dsl41.runner_adapters import FileWatcherAdapter
    from dsl41.semantics import fw_existence_immediate

    job = job_ir.name
    if run_dir is None and engine.run_root is not None:
        # the preflight's fallback: a candidate that came from a dispatch record without a
        # run_dir still has one, and without it the inject-from-log branch below is unreachable
        run_dir = engine.run_root / "runs" / f"{job}.{run_number}"
    watch = read_watch_log(run_dir) if run_dir is not None else None
    if watch is not None and bound is not None:
        # re-checked at this read, not only at the preflight: a log that appeared in the window
        # between them is held to the same identity, or its fate would be injected as this run's
        _refuse_identity_split(job, run_number, bound, watch.run_id, "the spool's watch.jsonl")
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
    if not relaunch or bound is None:
        # a watch is relaunched only under the identity of a start this log dispatched (DL-315)
        return
    # idempotent read: the adapter reconstructs progress from the log and appends no second
    # `start` line. The bound id rides along for the case with no log to reconstruct from (a run
    # directory made, then a crash before the `start` line), where a watch dispatched with no
    # identity would write `run_id: null` and split from the WAL (DL-118).
    engine._launch(job_ir, run_number, adapter, run_id=bound)


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
    engine: Engine,
    candidates: ResumeEvidence,
    last_at: datetime,
    dispatched: Mapping[tuple[str, int], str],
    spawns: frozenset[tuple[str, int]] | set[tuple[str, int]],
) -> None:
    """Live starts with no run directory (S6c, DL-102).

    The ladder above asks how runs that left a trace ended; this asks what to do about a start
    that left none. The log answers, because a start's `dispatch` record is durable before its
    adapter task exists and its run directory is made after (ss7):

    - a `dispatch` record: the start was launched and the crash took it before it made its
      directory. A tethered command is FAILURE, because a run provably never started is never
      silently re-executed; a watch is re-dispatched under its recorded `run_id`, because
      polling is an idempotent read (DL-44 item 7);
    - no record, and replay derives the start (`spawns`): the previous engine decided it and
      died before dispatching it, or the attempt's decision never landed. Nothing ran, so the
      loop dispatches it once the reconciliation inputs and the overdue timers have applied,
      through the same supersession check a live start passes (`Engine.recover_start`);
    - neither: the live engine launched nothing for this run and never would, such as a
      STARTING overwrite at a run that never started. The row stays live, for the operator to end
      or restart (DL-315)."""
    for job, rt in engine.oracle.store.job.items():
        if rt.status not in LIVE or (job, rt.run_number) in candidates:
            continue
        job_ir = engine.oracle.catalog.jobs.get(job)
        if job_ir is None or job_ir.job_type == "BOX":
            continue  # boxes fold from members; pseudo-entries have no dispatch
        bound = dispatched.get((job, rt.run_number))
        if bound is None:
            if (job, rt.run_number) in spawns:
                engine.recover_start(job, rt.run_number, after=last_at)
            continue
        if job_ir.job_type == "FW":
            adapter = engine.adapters.get("FW")
            if adapter is None:
                # _require_adapters runs at genesis and at resume, before reconciliation, and
                # refuses a catalog that runs an FW job with no FW adapter wired. This branch is
                # reached only for job_ir.job_type == "FW", so that adapter is present.
                raise AssertionError(
                    "_require_adapters already refuses a resume with an FW job and no"
                    " FW adapter wired, so adapter cannot be None here"
                )
            engine._launch(job_ir, rt.run_number, adapter, run_id=bound)
            continue
        _inject_completion(
            engine,
            job,
            rt.run_number,
            {"status": "FAILURE", "cause": "dispatch lost to engine crash (never spawned)"},
            at=last_at,
            last_at=last_at,
        )


def _refuse_identity_split(
    job: str, run_number: int, bound: str | None, observed: object, source: str
) -> None:
    """One key runs through the WAL and the spool (DL-118, PR-36a): the `dispatch` record bound
    this run's `run_id` before its wrapper existed, so present evidence about the run must name
    that id. `bound` None is a start the log decided and never dispatched, which resume is about
    to dispatch under a fresh id, so any evidence for it is a stranger's. A different id, or none
    where a wrapper always records one, is a WAL/spool split: corruption, a spoofed record, or a
    directory from another estate, and reattaching to, resolving, or killing that process would
    act on a run the log never spawned. It refuses loudly; there is no correct pick between two
    identities for one run. Callers pass only evidence that exists: an absent file is the
    ladder's business."""
    observed_id = str(observed) if observed is not None else None
    if bound is None:
        raise EngineError(
            f"{job}.{run_number}: {source} reports run_id {observed_id!r} but the log decided"
            " this start and never dispatched it -- refusing to act on a run the log did not"
            " spawn (DL-118)"
        )
    if observed_id != bound:
        raise EngineError(
            f"{job}.{run_number}: {source} reports run_id"
            f" {observed_id!r} but the dispatch record bound {bound!r}"
            " -- refusing to act on a run the log did not spawn (DL-118)"
        )


def _preflight_identities(
    engine: Engine,
    candidates: ResumeEvidence,
    dispatched: Mapping[tuple[str, int], str],
    spawns: frozenset[tuple[str, int]] | set[tuple[str, int]],
) -> None:
    """Check every candidate's observed identities against the WAL before resume mutates
    anything (DL-118). One sweep up front: branch-by-branch checks leave ordering holes (a
    reconciliation that injected one run's fate before a later branch refused the same run leaves
    the refusal half-taken).

    A run is bound by its `dispatch` record, or is a start replay derives (`spawns`) that resume
    would dispatch. A run that is neither, such as one an operator's STARTING overwrite made
    live, has no identity to hold its evidence to, and is checked for nothing (DL-315).

    Resume runs this before it writes the recovered decisions, so a refusal there has written
    nothing. The ladder runs it again over the evidence as it then stands, and it passes unless
    the spool changed in between."""
    for (job, run_number), directory in sorted(candidates.items()):
        bound = dispatched.get((job, run_number))
        if bound is None and (job, run_number) not in spawns:
            continue
        for name in ("spawn.json", "status.json"):
            doc = load_json(directory / name)
            if doc is not None:
                _refuse_identity_split(
                    job, run_number, bound, doc.get("run_id"), f"the spool's {name}"
                )
        # an FW watch spawns no process; its `start` line names the run and is present
        # evidence like any other
        watch = read_watch_log(directory)
        if watch is not None:
            _refuse_identity_split(job, run_number, bound, watch.run_id, "the spool's watch.jsonl")
