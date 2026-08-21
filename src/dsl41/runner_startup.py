"""Taking possession of a run root (ss7): genesis, resume, and the barrier.

Split out of runner.py by DL-106, with the paragraph it owns, verbatim.
That module's own docstring has said "the engine loop and the run
lifecycle" since DL-74; this is the half after the "and". The Engine is the
single-writer loop over a live estate. What lives here runs ONCE per
incarnation, before that loop exists: it creates or claims a run root,
takes leadership of it, replays the log, reconciles the estate against the
world, and hands back an Engine ready to run. The two halves share exactly
one object, and it is the one this half constructs.

Not "lifecycle" in the name: DL-42 spent that word on the wrapper and
supervisor tier, and two meanings of it in one codebase is how a reader
learns to check which one is meant.

- Resume (ss7): refuse on catalog-hash or clock-domain mismatch, replay
  inputs through a fresh Oracle, seed the ghost-run gate so replayed starts
  never respawn, then reconcile from the spool ladder: live wrapper ->
  settle window; status.json -> inject the real completion at
  max(ended_at, last journal at) with the true ended_at in the payload;
  verified command group orphaned by a dead wrapper -> kill it, TERMINATED
  "wrapper lost; killed at resume" (a kill that happened); nothing ->
  FAILURE exit_status_unobservable (PENDING: E7). A start with no trace
  anywhere splits in two (DL-102): one whose SPAWN is still PENDING in the
  outbox is an intent the previous leader never delivered and is re-driven;
  one with no pending intent is FAILURE "dispatch lost to engine crash" --
  provably-never-ran is still never re-executed silently. FW watchers are
  the exception to both: polling is an idempotent read, so incomplete FW
  runs are re-dispatched. Reconciliation completions go through the ss4
  stale gate like any adapter completion: if replay already reached a
  terminal state (say a term_run_time TERMINATED), the late real record is
  dropped AND journaled -- never a silent overwrite.

The whole sequence IS concurrency-model ss7's takeover barrier -- ACQUIRE,
reconcile every execution host, retire superseded and re-drive pending,
dispatch -- and `start_run` is its degenerate case: an empty log has
nothing to replay, nothing to reconcile and nothing to re-drive, so all
that survives of the barrier is the acquire.
"""

from __future__ import annotations

import contextlib
import os

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from dsl41 import runner_procid as _procid
from dsl41.ir import CatalogIR, JobIR
from dsl41.oracle_state import Event, TERMINAL
from dsl41.runner import Engine
from dsl41.runner_adapters import (
    JobAdapter,
    fsync_dir,
    SupervisedCommandAdapter,
    SupervisorClient,
    SupervisorUnavailable,
    Terminated,
    load_json,
    outcome_from_status,
    read_watch_log,
    resolve_spool,
)
from dsl41.runner_clock import Clock, EngineError
from dsl41.runner_effects import Effect, EffectOutcome
from dsl41.period import (
    GENESIS_PERIOD_ID,
    GENESIS_SEGMENT_NO,
    RuntimeProfile,
    Sentinel,
    StagedManifest,
    estate_wal,
    check_manifest_against_segment,
    genesis_manifest,
    read_period_manifest,
    opening_at,
    read_sentinel,
    wal_path,
    wal_segments,
    write_period_manifest,
    split_run_dir,
)
from dsl41.runner_journal import (
    Journal,
    last_journal_at,
    baseline_id,
    read_journal,
    repair_tail,
    scheduler_frontier,
    replay_inputs,
)
from dsl41.boundary import (
    CommittedBoundary,
    EstateAnchor,
    EstateHome,
    OpenedPeriod,
    act_on_head,
    carried_outbox,
    claim_root,
    default_anchor_dir,
    open_next_period,
    open_wal,
    seal_record,
    select_seal,
)
from dsl41.oracle_state import CarriedRows
from dsl41.seal import OpenedRuntime, Seal, open_from_seal
from dsl41.runner_ledger import (
    STATE_MACHINE_VERSION,
    Fence,
    Proof,
    LeaderLock,
    acquire_run_root,
    check_leader_eligibility,
    next_epoch,
)
from dsl41.runner_scheduler import Scheduler


def _derive_runtime_profile(
    scheduler: Scheduler | None,
    adapters: Mapping[str, JobAdapter],
    deadman_s: float | None,
    base: RuntimeProfile | None,
) -> RuntimeProfile:
    """The runtime profile the engine is ACTUALLY wired with (period-model
    ss2.1, DL-130).

    The pin has to describe the machine that runs, not the flags somebody
    typed: an embedder that builds a Europe/Zurich scheduler and stages
    nothing would otherwise open a period whose hash says UTC while every
    tick is shifted. So the wired components are read back -- the
    scheduler's timezone, the adapters' modes and windows, the deadman --
    over `base` for the fields the engine cannot see (machine policy and
    as-machine live in preflight, not on any wired object)."""
    from dsl41.period import RuntimeProfile, to_us
    from dsl41.runner_adapters import FileWatcherAdapter, LocalCommandAdapter

    values: dict[str, object] = dict((base or RuntimeProfile()).model_dump())
    if scheduler is not None:
        values["default_tz"] = scheduler.default_tz or "UTC"
        values["tz_aliases"] = dict(scheduler.tz_aliases)
    values["deadman_us"] = None if deadman_s is None else to_us(deadman_s)
    cmd = adapters.get("CMD")
    if isinstance(cmd, SupervisedCommandAdapter):
        values["execution_mode"] = "detached"
        values["cmd_grace_us"] = to_us(cmd.grace_seconds)
        values["reconcile_settle_us"] = to_us(cmd.settle_seconds)
    elif isinstance(cmd, LocalCommandAdapter):
        values["execution_mode"] = "tethered"
        values["cmd_grace_us"] = to_us(cmd.grace_seconds)
    fw = adapters.get("FW")
    if isinstance(fw, FileWatcherAdapter):
        values["fw_default_interval_us"] = to_us(float(fw.default_interval_s))
    # the spawn window is a module constant, not an adapter knob: derive it
    # from the value the machine actually runs, so a staged 0 cannot pin a
    # fiction over the real five seconds
    values["spawn_window_us"] = to_us(SupervisedCommandAdapter._SPAWN_WINDOW_S)
    return RuntimeProfile.model_validate(values)


def _require_scheduler(catalog: CatalogIR, scheduler: Scheduler | None, where: str) -> None:
    """A catalog that schedules jobs needs a scheduler wired -- and the
    RIGHT one (DL-130). The profile cannot see a scheduler's absence
    (default_tz inherits the pin and reports no drift), and one built over
    a DIFFERENT catalog carries another estate's plans with a matching
    timezone -- either way scheduled execution silently stops or fires
    wrong. Run at genesis and at resume."""
    if scheduler is not None:
        # EVERY supplied scheduler is held to its compile-time hash --
        # before the scheduled-jobs check, because a catalog whose last
        # trigger was removed after the compile would otherwise skip the
        # comparison while the stale plans still fire the removed jobs
        from dsl41.period import catalog_hash_v2

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
    """Every executable job type must have an adapter wired (DL-130). The
    profile inherits pinned values for wiring it cannot see, so a missing
    adapter drifts nothing -- and a job of that type then reaches RUNNING
    with no process behind it (`plan_effects` suppresses the SPAWN for a
    type with no dispatch row). Run at GENESIS and at resume: an estate
    that opens unable to dispatch its own catalog is the same silent hole
    one gate later."""
    required = sorted(
        {job.job_type for job in catalog.jobs.values() if job.job_type != "BOX"} - set(adapters)
    )
    if required:
        raise EngineError(
            f"this catalog runs job type(s) {', '.join(required)} and the {where}"
            " wired no adapter for them -- their jobs would run with no process"
            " behind them (period-model ss2.1)"
        )


def _profile_drift(derived: RuntimeProfile, pinned: RuntimeProfile) -> list[str]:
    return sorted(
        name
        for name in type(derived).model_fields
        if getattr(derived, name) != getattr(pinned, name)
    )


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
    anchor_dir: Path | None = None,
) -> Engine:
    """ss1.1's GENESIS TRANSACTION, in its order, plus an Engine wired to
    what it created.

    Six ordered steps, and the order is the recovery argument:

    1. `flock` `leader.lock` -- before the refusal, not after: the refusal
       reads the estate's state, and one leader per run root is the rule
       under which any such read is meaningful (DL-99);
    2. the `period_root` SENTINEL, create-only under ss1.1's ownership
       rule. It is the first durable act, so there is no instant at which
       this root looks unused to an old binary -- which would start a fresh
       genesis beside the lineage and admit work while detached C1
       executions were still alive;
    3. `anchor.lock` and the create-only CAS `absent -> open(1, root)`;
    4. the bundle and `periods/000001/manifest.json`;
    5. `wal/000001.jsonl` with its `segment` record;
    6. the finalize CAS setting `periods[1].segment_durable = true`.

    A crash after step 2 and before step 5 leaves a root no old binary can
    use and that a RE-RUN of genesis completes idempotently, reading
    `estate_id` back from the sentinel rather than minting a second
    (PR-01a). Once a segment exists, ordinary `--resume` owns recovery and
    this refuses -- that is what `--resume` is for, and no silent
    re-baselining.

    The manifest is installed before the log opens, and both are derived
    from ONE object so the two cannot disagree (PR-22). `staged` is what
    the launcher pinned -- catalog hash, bundle address, runtime profile; a
    caller with none gets the default profile over the empty bundle.
    `anchor_dir` defaults to the root's sibling (`boundary.default_anchor_dir`),
    which is outside every archivable root, as ss1.1 requires. A failure
    here releases both locks, because a caller that got this far and was
    refused is on its way out."""
    # the root is CREATED by acquire_run_root (mkdir_durable: every new
    # entry fsynced), never pre-created here -- a plain mkdir first would
    # make the durability helper see an existing directory and prove
    # nothing about its dirent
    lock = lock or acquire_run_root(run_root)
    # the run root holds the journal (global values, every control input),
    # job output, and data -- owner-only, loudly, not umask-hopefully
    os.chmod(run_root, 0o700)
    anchor = EstateAnchor(anchor_dir or default_anchor_dir(run_root))
    try:
        root = claim_root(run_root)  # step 2
        if wal_path(run_root, GENESIS_SEGMENT_NO).exists():
            raise EngineError(
                f"{wal_path(run_root, GENESIS_SEGMENT_NO)} already exists: resume it"
                " (resume_run) or pick a fresh run root"
            )
        anchor.acquire()
        anchor.create_open(estate_id=root.estate_id, root=run_root)  # step 3
    except BaseException:
        anchor.release()
        lock.release()
        raise
    fence = Fence(lock, anchor.lock)
    try:
        return _finish_genesis(
            catalog,
            run_root,
            lock,
            anchor,
            fence,
            root_estate_id=root.estate_id,
            clock=clock,
            adapters=adapters,
            scheduler=scheduler,
            hold_open=hold_open,
            deadman_s=deadman_s,
            staged=staged,
        )
    except BaseException:
        # a refused genesis holds nothing (same rule as the claim above):
        # both raw-fd locks conflict with a retry in this same process, so
        # leaving them held wedges every embedder and test that retries
        anchor.release()
        lock.release()
        raise


def _finish_genesis(
    catalog: CatalogIR,
    run_root: Path,
    lock: LeaderLock,
    anchor: EstateAnchor,
    fence: Fence,
    *,
    root_estate_id: str,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    scheduler: Scheduler | None,
    hold_open: bool,
    deadman_s: float | None,
    staged: StagedManifest | None,
) -> Engine:
    """ss1.1 steps 4-6 plus the Engine, split out so `start_run` can pair
    the acquire with a release on every failure path."""
    (run_root / "runs").mkdir(exist_ok=True)
    (run_root / "logs").mkdir(exist_ok=True)
    at = clock.now()
    _require_adapters(catalog, adapters, "genesis")
    _require_scheduler(catalog, scheduler, "genesis")
    derived = _derive_runtime_profile(
        scheduler, adapters, deadman_s, base=staged.runtime_profile if staged else None
    )
    if staged is not None:
        if staged.state_machine_version != STATE_MACHINE_VERSION:
            # one executable implements exactly one state-machine version
            # (period-model ss2.1): committing a different pin would leave
            # this engine running beneath a manifest it can never satisfy
            raise EngineError(
                f"staged state_machine_version {staged.state_machine_version}: this"
                f" build runs {STATE_MACHINE_VERSION}"
            )
        drift = _profile_drift(derived, staged.runtime_profile)
        if drift:
            # the pin must describe the machine that runs: a staged profile
            # the wiring disagrees with is a fiction, refused before it is
            # made durable
            raise EngineError(
                f"staged runtime profile disagrees with the engine's wiring on"
                f" {', '.join(drift)} (period-model ss2.1)"
            )
    else:
        from dsl41.period import EMPTY_BUNDLE_HASH, stage_manifest

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
    write_period_manifest(run_root, manifest)  # step 4
    journal = Journal.create(  # step 5
        open_wal(run_root, GENESIS_SEGMENT_NO),
        catalog=catalog,
        clock_domain=manifest.clock_domain,
        started_at=at,
        lock=fence,
        manifest=manifest,
        estate_id=root_estate_id,
    )
    epoch = next_epoch([])  # the first term over a log that has none
    journal.leader(epoch=epoch, at=at)
    lock.note(epoch=epoch, at=at)
    fsync_dir(journal.path.parent)  # the segment's directory entry is a record too
    fsync_dir(run_root)
    anchor.finalize(GENESIS_PERIOD_ID)  # step 6
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
        estate=EstateHome(
            run_root=run_root,
            anchor=anchor,
            estate_id=root_estate_id,
            manifest=manifest,
        ),
        fence=fence,
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
    supervisor: SupervisorClient | None = None,
    deadman_s: float | None = None,
    lock: LeaderLock | None = None,
    anchor_dir: Path | None = None,
) -> Engine:
    """ss7 resume: hash-gate, replay, reconcile. Returns an Engine with the
    reconciliation completions queued (source=reconcile); the caller runs
    the loop to process them and continue the run.

    A `scheduler` is re-anchored at the last journal instant INCLUSIVE and
    deduped against the journal's own scheduler ticks (a crash between
    same-instant siblings' appends must lose none of them silently); the
    unjournaled remainder of the window up to wall-now was missed
    across downtime and is dropped AND journaled -- reported on
    Engine.drops, never fired late (PENDING: E9; a live-but-stalled engine
    fires its backlog, downtime never does)."""
    os.chmod(run_root, 0o700)  # tighten a pre-existing looser root (same reason as create)
    # ACQUIRE first (S6a, concurrency-model ss7): everything below this line
    # reads or acts -- the log is replayed, the estate is reconciled,
    # recorded kills are re-driven -- and a mutex taken after the first side
    # effect is not a mutex. It also has to precede the READ, or another
    # engine could append between the read and the acquire and this one would
    # allocate an epoch the log already used.
    lock = lock or acquire_run_root(run_root)
    # ss11 steps 1-2, here rather than below, so the acquire/release pairing
    # of BOTH proofs is one readable block: a refused resume must hold
    # neither, or the next engine cannot lead a lineage this one could not.
    anchor: EstateAnchor | None = None
    try:
        sentinel = read_sentinel(run_root)
        # a root whose `journal.jsonl` is not a sentinel gets its refusal
        # from the reader that owns the question: `read_journal`'s record
        # tombstone below, or `claim_root`'s (D1/D5, DL-138). This resume
        # kept a second copy of that judgement and no longer needs one.
        if sentinel is not None:
            anchor = EstateAnchor(anchor_dir or default_anchor_dir(run_root))
            anchor.acquire()
            anchor.require(sentinel.estate_id)
        engine = await _resume_under_lock(
            catalog,
            run_root,
            lock,
            sentinel=sentinel,
            anchor=anchor,
            clock=clock,
            adapters=adapters,
            scheduler=scheduler,
            hold_open=hold_open,
            settle_seconds=settle_seconds,
            grace_seconds=grace_seconds,
            supervisor=supervisor,
            deadman_s=deadman_s,
        )
    except BaseException:
        # a refused resume holds nothing: the next engine may lead
        if anchor is not None:
            anchor.release()
        lock.release()
        raise
    return engine


async def _resume_under_lock(
    catalog: CatalogIR,
    run_root: Path,
    lock: LeaderLock,
    *,
    sentinel: Sentinel | None,
    anchor: EstateAnchor | None,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    scheduler: Scheduler | None,
    hold_open: bool,
    settle_seconds: float | None,
    grace_seconds: float | None,
    supervisor: SupervisorClient | None,
    deadman_s: float | None,
) -> Engine:
    """The ss7 resume ladder proper, with leadership already held (S6a).
    Split from `resume_run` so the acquire/release pairing is one readable
    block rather than a `finally` wrapped around a hundred lines.

    ss11 steps 1-4 run first, and in their order: the sentinel (ss1.1's
    ownership rule applies to resume as to creation), the anchor and its
    `estate_id`, the SEAL selected by lineage from what this root holds,
    and then the head action that repairs whatever window the last process
    died in. Only then does the ladder this function had before begin --
    over the segment the lineage selected, which on a committed boundary is
    the one this resume just opened. Steps 1 and 2 -- the sentinel and the
    anchor -- are the CALLER's, because they are the other half of the
    acquire/release pairing."""
    fence = Fence(lock, anchor.lock) if anchor is not None else Fence(lock)
    _drop_never_opened_segment(run_root)
    records = read_journal(estate_wal(run_root))
    lineage = select_seal(run_root, records)  # step 3
    if anchor is not None and sentinel is not None:
        act_on_head(  # step 4
            anchor,
            run_root=run_root,
            estate_id=sentinel.estate_id,
            lineage=lineage,
        )
    opened_period: OpenedPeriod | None = None
    journal: Journal | None = None
    if lineage.opens_next:
        assert anchor is not None and lineage.seal is not None
        opened_period = _open_from_seal(run_root, anchor, lineage.seal, catalog, fence)
        journal = opened_period.journal
        records = read_journal(journal.path)
    opening = records[0]
    # ss7 phase 3, at EVERY resume of a period that opened from a seal --
    # not only at the resume that opened it. The segment is written once
    # and resumed many times, and an opener that seeded the carry only on
    # the opening pass rebuilt every later incarnation from the CATALOG:
    # globals gone, holds gone, revisions moved, and an `effect_result`
    # for a carried effect refused as an outcome for an unknown effect.
    # The seal is the same one either way, and phase 3 is a pure function
    # of it, so there is one code path and it runs unconditionally.
    opened = (
        opened_period.opened
        if opened_period is not None
        else _reopened(run_root, opening, lineage.seal)
    )
    carried: CarriedRows | None = None if opened is None else _carried_rows(opened)
    check_leader_eligibility(opening, catalog=catalog)
    # PR-22's U4 half: the committed manifest is the engine's own output,
    # and a manifest that is not this segment's refuses rather than being
    # read past. A MISSING artifact degrades where a WRONG one refuses
    # (DL-113 decision 5) -- but not here: genesis installs the manifest
    # BEFORE the log opens, so a root without one is a root that LOST it,
    # pruned or damaged, and degrading would skip every profile gate below.
    # The `header` root that once degraded here is a retired dialect and
    # `read_journal` refused it above (DL-138).
    # `read_journal` has run ss2.1's schema over the opening, so `period_id`
    # is present and an int -- a fallback here would be a second authority
    # for a field the reader already proved (DL-138)
    manifest = read_period_manifest(run_root, int(opening["period_id"]))
    if manifest is None:
        raise EngineError(
            f"{run_root}: a segment journal with no periods/000001/manifest.json --"
            " the period's pin is missing (period-model ss2.1)"
        )
    check_manifest_against_segment(manifest, opening)
    # the runtime half of the same gate, in CORE: the wired components
    # must be what the period pinned, or shifted ticks and different
    # kill windows run under an unchanged hash. Fields the engine
    # cannot see inherit the pin, so only real wiring can move this.
    derived = _derive_runtime_profile(scheduler, adapters, deadman_s, base=manifest.runtime_profile)
    drift = _profile_drift(derived, manifest.runtime_profile)
    if drift:
        raise EngineError(
            f"runtime-profile mismatch on {', '.join(drift)}: a runtime-profile"
            " change is a new period (period-model ss2.1); re-baseline"
            " explicitly with a fresh run root"
        )
    # the reconciliation windows have no wire flag, so the PIN is their
    # default: a None param resolves from the manifest, and an explicit
    # one is a caller's deliberate override (a harness affordance, like
    # wiring a different adapter object)
    if settle_seconds is None:
        settle_seconds = manifest.runtime_profile.reconcile_settle_us / 1_000_000
    if grace_seconds is None:
        grace_seconds = manifest.runtime_profile.cmd_grace_us / 1_000_000
    # unconditional (DL-137): the guard's docstring always said "run at
    # genesis and at resume", and a manifest-less root never reaches this
    # line -- the condition was a dead fork of the stated rule
    _require_adapters(catalog, adapters, "resume")
    _require_scheduler(catalog, scheduler, "resume")
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
    if journal is None:
        journal = Journal(
            estate_wal(run_root),
            fsync_each=not clock.virtual,
            baseline_id=baseline_id(records),
            lock=fence,
        )
    # the term is allocated by being appended (ss1), before the first input
    # this incarnation admits, so every record after it names its author.
    # I2 makes the epoch estate-monotone, so a new period's first term is
    # `seal.epoch + 1` and never 1 again (ss2.4)
    epoch = max(next_epoch(records), lineage.seal.epoch + 1 if lineage.seal is not None else 1)
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
        carried=carried,
        estate=(
            EstateHome(
                run_root=run_root,
                anchor=anchor,
                estate_id=sentinel.estate_id,
                manifest=manifest,
                prev_seal_digest=lineage.seal.digest if lineage.seal is not None else None,
                prior_seal_record=(seal_record(lineage.seal) if lineage.seal is not None else None),
            )
            if anchor is not None and sentinel is not None and manifest is not None
            else None
        ),
        fence=fence,
    )
    # ss3.5: the carry is what this segment OPENED holding -- C1's
    # undelivered intents and its applied bindings -- and it is seeded
    # BEFORE the segment's own records are read, never patched in
    # afterwards: an `effect_result` here for an effect born in C1 is an
    # outcome the replay has to attach, and `Outbox.resolve` refuses an
    # outcome for an effect it never saw.
    replay = replay_inputs(
        engine.oracle, records, outbox=carried_outbox(opened, at=opening_at(opening))
    )
    # the log's position comes back with its contents (concurrency-model
    # ss2): the next admission continues the index, and a retry of anything
    # this log already decided is still answered from that decision rather
    # than applied a second time
    engine.frontiers = replay.frontiers
    engine.decisions = replay.decisions
    # ss5: the effects the previous engine intended, and what became of them.
    # An engine that forgot a kill it had decided would leave a detached run
    # orphaned for the rest of its life -- its job is already TERMINAL, so
    # reconciliation skips it, and nothing else would ever look again.
    engine.outbox = replay.outbox
    # seed the ghost-run gate: replayed starts are reconciliation's business,
    # never a fresh dispatch
    for job, rt in engine.oracle.store.job.items():
        if rt.run_number:
            engine._dispatched[job] = rt.run_number
    if scheduler is not None:
        # re-anchor INCLUSIVE of last_at and dedup against the ticks the
        # journal actually holds: with several jobs scheduled at one instant,
        # a crash between the siblings' input appends leaves last_at == tick
        # with a sibling unjournaled -- an exclusive re-anchor would lose it
        # silently, with no drop record (DL-45). Journaled ticks
        # were fed by replay and are skipped; the rest of the due window is
        # dropped AND journaled, never fired late.
        replayed_ticks = {
            (record["payload"].get("job"), record["at"])
            for record in records
            if record.get("rec") == "input"
            and record.get("source") == "scheduler"
            and record.get("kind") == "STARTJOB"
        }
        frontier = scheduler_frontier(records)
        # ss6 step 9: a period opened from a seal starts its scheduler
        # STRICTLY AFTER T. C1's cutoff admitted every tick due at or
        # before T -- that is what `scheduler_admitted_through` records --
        # so an inclusive re-anchor at T would re-derive the tick C1 just
        # ran and journal a `drop` saying the engine missed it. The
        # inclusive anchor is for the OTHER case it was written for: a
        # crash between same-instant siblings' appends, where the frontier
        # is a tick this segment actually holds.
        opened_at_t = opening.get("opens_from_seal") is not None and frontier == opening_at(opening)
        scheduler.reset(frontier, inclusive=not opened_at_t)
        sweep_upto = max(clock.now(), frontier)  # virtual resume: now < the frontier
        for tick_ev in scheduler.pop_due(sweep_upto):
            if (tick_ev.job(), tick_ev.at.isoformat()) in replayed_ticks:
                continue  # replay already fed this tick
            reason = "scheduler tick missed while the engine was down; not fired late"
            engine.drops.append((tick_ev, reason))  # PENDING: E9
            journal.drop(tick_ev, reason)
    # ss8's supervisor proof needs the CLIENT on the engine, and an
    # embedder that resumes with one must not depend on the CLI's wiring
    # for the seal to prove anything (PR-27)
    engine.supervisor = supervisor
    await _reconcile(
        engine,
        records,
        last_at,
        settle_seconds=settle_seconds,
        grace_seconds=grace_seconds,
        supervisor=supervisor,
    )
    return engine


def _drop_never_opened_segment(run_root: Path) -> None:
    """ss11's matrix row: a torn or empty FIRST line means the segment
    never opened -- re-open it from the boundary.

    Such a file holds nothing a reader can use and nothing a writer may
    append to: appending would put a second `segment` record in one file,
    which is exactly the two-candidate state I1 exists to make impossible.
    Removing it is safe because the opening is a pure function of the seal,
    so what replaces it is byte-identical (PR-07). Only ever the NEWEST
    segment, and only when an earlier one exists -- period 1 has no
    boundary to re-open from, and its own empty-log case is genesis's."""
    segments = wal_segments(run_root)
    if len(segments) < 2:
        return
    path = wal_path(run_root, segments[-1])
    repair_tail(path)  # a torn final line goes, exactly as replay drops one
    if path.stat().st_size:
        return
    path.unlink()
    fsync_dir(path.parent)


def _open_from_seal(
    run_root: Path,
    anchor: EstateAnchor,
    seal: Seal,
    catalog: CatalogIR,
    fence: Proof,
) -> OpenedPeriod:
    """ss11 step 5's opening half, in place: claim the successor and write
    the opening segment from the seal this root closed with.

    The committed manifest must already be installed -- the boundary
    renamed it into `periods/N+1/` BEFORE the record that names it -- so a
    missing one is a boundary whose artifacts were pruned under the head,
    which the retention floor forbids and which recovery cannot invent."""
    opening = seal.next_period
    manifest = read_period_manifest(run_root, opening.period_id)
    if manifest is None:
        raise EngineError(
            f"{run_root}: seal {seal.digest} commits period {opening.period_id} and"
            f" periods/{opening.period_id:06d}/manifest.json is not there -- the"
            " boundary's own artifacts are reachable from the lineage head and may"
            " never be pruned (period-model ss12)"
        )
    return open_next_period(
        run_root=run_root,
        anchor=anchor,
        committed=CommittedBoundary(seal=seal, manifest=manifest),
        catalog=catalog,
        lock=fence,
    )


def _reopened(
    run_root: Path, opening: Mapping[str, Any], seal: Seal | None
) -> OpenedRuntime | None:
    """ss7 phase 3 over a segment that is ALREADY open: the carry this
    period was seeded from the first time it opened.

    None for a genesis segment -- it opened from no seal, so it has no
    carry. `select_seal` has already verified the sidecar against the digest
    the segment names, and this re-runs the pure load over it, because the
    load is what turns a sidecar into rows and it is the same load either
    way."""
    link = opening.get("opens_from_seal")
    if seal is None or not isinstance(link, Mapping):
        return None
    manifest = read_period_manifest(run_root, int(opening["period_id"]))
    if manifest is None:
        raise EngineError(
            f"{run_root}: periods/{int(opening['period_id']):06d}/manifest.json is not"
            " there -- a period that opened from a seal is re-seeded from that seal at"
            " every resume, and the load needs this period's committed manifest"
            " (period-model ss7 phase 3)"
        )
    return open_from_seal(seal, expected_digest=str(link["digest"]), manifest=manifest)


def _carried_rows(opened: OpenedRuntime) -> CarriedRows:
    """The sidecar's carried half -- `OpenedRuntime.carried_rows`, the ONE
    derivation (DL-137): a carried field added to `CarriedRows` is spelled
    once, or the engine and the auditor open the same period holding
    different state."""
    return opened.carried_rows


async def _reconcile(
    engine: Engine,
    records: list[dict[str, Any]],
    last_at: datetime,
    *,
    settle_seconds: float,
    grace_seconds: float,
    supervisor: SupervisorClient | None = None,
) -> None:
    """The ss6a/ss7 reconciliation ladder (module docstring). Tethered
    semantics did the killing already (wrappers EOF'd when the engine
    died), so this is mostly READING; signals are for the residual crash
    matrix only, and only ever at a (pid, start-time)-verified target.

    Detached resume (spec ss3): with a `supervisor`, an in-flight run the
    supervisor still LISTs as wrapper_alive is REATTACHED -- the adapter task
    just awaits its exit push, no reconciliation injection (the run never
    stopped, E4 dissolved). Runs listed dead or unlisted fall through to the
    spool ladder unchanged (the supervisor died, or the run predates it)."""
    assert engine.run_root is not None
    boot_now = _procid.current_boot_id()
    supervised_live: dict[tuple[str, int], dict[str, Any]] = {}
    if supervisor is not None:
        with contextlib.suppress(SupervisorUnavailable):
            listing = await supervisor.list_runs()
            supervised_live = {
                (str(r["job"]), int(r["run_number"])): r for r in listing.get("runs", [])
            }
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
                # the CANONICAL parser (DL-137): the inline rpartition it
                # replaces accepted `b.01` as run 1, and setdefault over the
                # sorted listing then let a directory this estate never
                # wrote answer the ss7 ladder for a real run's fate
                candidates.setdefault(run, entry)
    # ...and what the HOST says it is running (S6c). ss7 reconciles every
    # execution host, not every local directory: the sweep below concludes
    # "never spawned" from absence here, and absence that only means "the
    # run directory is gone" would let it re-drive a start the supervisor is
    # still running -- the double run the whole model exists to prevent.
    for key in supervised_live:
        candidates.setdefault(key, None)

    _preflight_identities(engine, candidates, supervised_live)
    _reconcile_applied_spawns(engine, candidates)

    for (job, run_number), run_dir in sorted(candidates.items()):
        rt = engine.oracle.store.job.get(job)
        if rt is None or rt.run_number != run_number or rt.status in TERMINAL:
            continue  # superseded run, or its completion already replayed
        job_ir = engine.oracle.catalog.jobs.get(job)
        if job_ir is None:
            continue
        reattach = supervised_live.get((job, run_number))
        if reattach is not None and reattach.get("wrapper_alive"):
            cmd_adapter = engine.adapters.get(job_ir.job_type)
            if isinstance(cmd_adapter, SupervisedCommandAdapter):
                # REATTACH: the run's parent (the supervisor) never died, so it
                # never stopped -- the adapter task just awaits its exit push,
                # NO reconciliation injection (spec ss3). The LIST row's
                # identity was checked against the WAL by the preflight.
                cmd_adapter.reattach[(job, run_number)] = str(reattach["run_id"])
                engine._launch(job_ir, run_number, cmd_adapter)
                continue
        if job_ir.job_type == "FW":
            _resume_watch(engine, job_ir, run_number, run_dir, last_at)
            continue
        bound = _spawn_effect_for(engine, job, run_number)
        cmd_adapter = engine.adapters.get(job_ir.job_type)
        if (
            isinstance(cmd_adapter, SupervisedCommandAdapter)
            and bound is not None
            and bound.run_id is not None
            and not _spool_has_evidence(run_dir)
        ):
            # PR-36a: the intent is durable and bound, the host holds NO
            # evidence a wrapper ever ran (no spawn.json, no status.json --
            # the supervisor may have died between its mkdir and the fork),
            # and SPAWN is idempotent now (ss11a). So the effect is REPLAYED
            # rather than guessed at: the supervisor answers first-application
            # (the run happens, once), duplicate (we await the run that
            # already did), in-progress (likewise), or indeterminate/collision
            # (the run fails naming the reason -- E7's policy, with the
            # supervisor's own words). The FAILURE verdict this branch used
            # to reach fabricated a fate for a run the host could still
            # answer for.
            engine._launch(job_ir, run_number, cmd_adapter, run_id=bound.run_id)
            continue
        # the ladder resolves this run's fate from its directory; the
        # directory's claim to BE this run's was checked by the preflight,
        # and the bound id rides along so a record that appears BETWEEN the
        # preflight and this read is held to the same identity
        result, ended_at = await resolve_spool(
            job,
            run_number,
            run_dir,
            boot_now,
            settle_seconds=settle_seconds,
            grace_seconds=grace_seconds,
            expected_run_id=bound.run_id if bound is not None else None,
        )
        extras: dict[str, object]
        if isinstance(result, int):
            extras = {"exit_code": result}
        elif isinstance(result, Terminated):
            extras = {"status": "TERMINATED", "cause": result.cause}
        else:
            extras = {"status": "FAILURE", "cause": result.cause}
        if ended_at is not None:
            extras["ended_at"] = ended_at.isoformat()  # true end time (ss7)
        _inject_completion(engine, job, run_number, extras, at=ended_at or last_at, last_at=last_at)

    _resume_untraced_starts(engine, candidates, last_at)
    await _redrive_recorded_kills(engine, supervised_live)
    # ss7's barrier ends where it says it does: ACQUIRE -> reconcile -> retire
    # superseded, re-drive pending -> DISPATCH. Without this the outbox is
    # drained only after the next admitted input (the loop dispatches on the
    # way out of `_admit_and_apply`), so a re-driven start would wait on
    # unrelated traffic to arrive -- hours, on a quiet estate, and never on
    # one whose only remaining work is the run that was lost. Everything
    # still pending here is a SPAWN nothing applied: the kills above are
    # resolved, and a SPAWN whose run reached the host was reconciled from
    # its trace. Superseded ones retire on the way through, and a drained or
    # quarantined host holds its own, in the one gate that owns that call.
    engine._dispatch()


def _spool_has_evidence(run_dir: Path | None) -> bool:
    """Whether a wrapper left any trace in this directory. The PR-36a replay
    is only for runs the host provably never recorded: the moment either
    record exists, the spool ladder owns the verdict."""
    if run_dir is None:
        return False
    return (run_dir / "spawn.json").exists() or (run_dir / "status.json").exists()


def _resume_watch(
    engine: Engine,
    job_ir: JobIR,
    run_number: int,
    run_dir: Path | None,
    last_at: datetime,
) -> None:
    """One incomplete FW run, from its spool (period-model ss2.2).

    A dispatched watch leaves a run directory now, so this is where a resumed
    watch lands: the sweep finds the directory, and the log says whether the
    watch is over."""
    job = job_ir.name
    if run_dir is None and engine.run_root is not None:
        # the same fallback the preflight makes: a candidate that came from a
        # dispatch record without a run_dir still has one, and without it the
        # inject-from-log branch below would never be reached
        run_dir = engine.run_root / "runs" / f"{job}.{run_number}"
    watch = read_watch_log(run_dir) if run_dir is not None else None
    bound = _spawn_effect_for(engine, job, run_number)
    if watch is not None and bound is not None:
        # re-checked at THIS read, not only at the preflight: a log that
        # appeared in the window between the two is held to the same
        # identity, or its fate would be injected as this run's
        _refuse_identity_split(bound, watch.run_id, "the spool's watch.jsonl")
    if watch is not None and watch.complete:
        # PR-34a: the last durable line is a completing observation and the row
        # is still RUNNING -- the engine died between the poll and the STATUS
        # input. Inject the completion FROM THE LOG, exactly as a CMD's is
        # injected from status.json; re-polling would decide the watch again
        # against a world that has moved on.
        extras: dict[str, object] = {"exit_code": 0}
        if watch.last_at is None:
            # WatchLog.complete requires stable_polls >= FW_STABLE_POLLS (2),
            # and read_watch_log derives stable_polls only from POLL lines,
            # so a complete watch has at least one poll line and last_at (set
            # from that line) cannot be None here (WatchLog.complete invariant).
            raise AssertionError(
                "a complete watch has at least one poll line, so last_at cannot"
                " be None (WatchLog.complete invariant)"
            )
        extras["ended_at"] = watch.last_at.isoformat()
        _inject_completion(engine, job, run_number, extras, at=watch.last_at, last_at=last_at)
        return
    adapter = engine.adapters.get("FW")
    if adapter is None:
        # _require_adapters runs at both genesis and resume, before
        # reconciliation ever reaches here, and refuses a catalog that runs
        # an FW job with no FW adapter wired. This function is reached only
        # for job_ir.job_type == "FW", so that adapter cannot be absent here.
        raise AssertionError(
            "_require_adapters already refuses a resume with an FW job and no FW"
            " adapter wired, so adapter cannot be None here"
        )
    # idempotent read: the adapter reconstructs progress from the log and
    # appends no second `start` line. The bound id rides along for the one
    # case with no log to reconstruct from -- a run directory made and then
    # crashed on, before the `start` line -- where a watch dispatched with no
    # identity would write `run_id: null` and split from the WAL (DL-118).
    engine._launch(job_ir, run_number, adapter, run_id=bound.run_id if bound else None)


def _inject_completion(
    engine: Engine,
    job: str,
    run_number: int,
    extras: dict[str, object],
    *,
    at: datetime,
    last_at: datetime,
) -> None:
    """One reconciliation verdict, as an input. `source="reconcile"` is what
    makes it a COMPLETION and therefore subject to the ss4 stale gate -- a
    verdict this engine reached about a run the log may already know the end
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
    """Starts with no trace anywhere -- no run directory, no dispatch record,
    and nothing the host admits to running (S6c).

    ss7's barrier says "retire superseded, re-drive pending", and this is
    where those four words become two cases. It is a separate question from
    the ladder above, which asks how runs that DID leave a trace ended; this
    one asks what to do about a decision that left none."""
    for job, rt in engine.oracle.store.job.items():
        if rt.status not in ("STARTING", "RUNNING") or (job, rt.run_number) in candidates:
            continue
        job_ir = engine.oracle.catalog.jobs.get(job)
        if job_ir is None or job_ir.job_type == "BOX":
            continue  # boxes fold from members; pseudo-entries have no dispatch
        if job_ir.job_type == "FW":
            adapter = engine.adapters.get("FW")
            if adapter is None:
                # _require_adapters runs at both genesis and resume, before
                # reconciliation ever reaches here, and refuses a catalog
                # that runs an FW job with no FW adapter wired. This branch
                # is reached only for job_ir.job_type == "FW", so that
                # adapter cannot be absent here.
                raise AssertionError(
                    "_require_adapters already refuses a resume with an FW job and no"
                    " FW adapter wired, so adapter cannot be None here"
                )
            bound = _spawn_effect_for(engine, job, rt.run_number)
            engine._launch(job_ir, rt.run_number, adapter, run_id=bound.run_id if bound else None)
            continue
        if engine.outbox.pending_for(job, "SPAWN"):
            # RE-DRIVEN. The log holds an intent to spawn that was never
            # resolved, and nothing anywhere ran: the previous leader died in
            # the window between recording what it meant to do and doing it.
            # Left pending, which is all re-driving takes -- `_dispatch`
            # drains the outbox the moment the loop runs, through the same
            # gates a fresh effect passes, so a drained or quarantined host
            # still HOLDS it (ss8) and this sweep does not need to know that.
            continue
        bound = _spawn_effect_for(engine, job, rt.run_number)
        adapter = engine.adapters.get(job_ir.job_type)
        if (
            isinstance(adapter, SupervisedCommandAdapter)
            and bound is not None
            and bound.run_id is not None
        ):
            # PR-36a again, with even less on disk: the intent is durable and
            # bound but nothing anywhere names a wrapper. Replaying through
            # the idempotent SPAWN is strictly safer than the FAILURE verdict
            # -- the supervisor's directory, not this engine's guess, says
            # whether the run already exists.
            engine._launch(job_ir, rt.run_number, adapter, run_id=bound.run_id)
            continue
        # FAILED. No pending intent, so the log never said a spawn was meant
        # to happen -- an effect already resolved whose spool has since gone
        # -- and either no supervisor path or no bound identity to replay
        # against. That is the case runner-design ss7 was reasoning
        # about when it chose to fail a start rather than silently re-run
        # it, and for these chains it still does.
        _inject_completion(
            engine,
            job,
            rt.run_number,
            {"status": "FAILURE", "cause": "dispatch lost to engine crash (never spawned)"},
            at=last_at,
            last_at=last_at,
        )


def _spawn_effect_for(engine: Engine, job: str, run_number: int) -> Effect | None:
    """The durable SPAWN that bound this run's identity, in any state --
    resolved included, because the split to refuse is between the WAL and
    what a spool or a supervisor claims NOW, and resolution does not expire
    the binding."""
    return next(
        (
            e
            for e in engine.outbox.effects()
            if e.kind == "SPAWN" and (e.job, e.run_number) == (job, run_number)
        ),
        None,
    )


def _refuse_identity_split(effect: Effect, observed: object, source: str) -> None:
    """One key runs through the WAL and the spool (DL-118, PR-36a): the
    durable effect bound this run's `run_id` at birth, so PRESENT evidence
    about the run must name that id. A different id -- or none, where a
    new-writer wrapper always records one -- is a WAL/spool split:
    corruption, a spoofed record, or a directory from another estate, and
    reattaching to, resolving, or killing that process would act on a run
    the log never spawned. Refused loudly rather than reconciled: there is
    no correct pick between two identities for one run. Callers pass only
    evidence that EXISTS -- an absent file is the ladder's business, not a
    split. A pre-DL-118 effect has no bound id and checks nothing."""
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
    supervised_live: dict[tuple[str, int], dict[str, Any]],
) -> None:
    """Check EVERY candidate's observed identities against the WAL before
    the barrier mutates anything (DL-118). One sweep, up front, because the
    branch-by-branch alternative had ordering holes by construction: a
    reconciliation that durably recorded `applied` before a later branch
    refused the same run left the refusal half-taken, and a dead LIST row
    with no local directory reached `resolve_spool` through a branch no
    guard covered. A refusal here has appended nothing and launched
    nothing -- the run root is exactly as the crash left it."""
    for (job, run_number), run_dir in sorted(candidates.items()):
        effect = _spawn_effect_for(engine, job, run_number)
        if effect is None or effect.run_id is None:
            continue
        directory = run_dir
        if directory is None and engine.run_root is not None:
            directory = engine.run_root / "runs" / f"{job}.{run_number}"
        if directory is not None:
            for name in ("spawn.json", "status.json"):
                doc = load_json(directory / name)
                if doc is not None:
                    _refuse_identity_split(effect, doc.get("run_id"), f"the spool's {name}")
            # an FW watch spawns no process; its `start` line is the record
            # that names the run, and it is present evidence like any other
            watch = read_watch_log(directory)
            if watch is not None:
                _refuse_identity_split(effect, watch.run_id, "the spool's watch.jsonl")
        listing = supervised_live.get((job, run_number))
        if listing is not None:  # alive or dead: a row is a claim either way
            _refuse_identity_split(effect, listing.get("run_id"), "the supervisor's LIST")


def _reconcile_applied_spawns(
    engine: Engine, candidates: dict[tuple[str, int], Path | None]
) -> None:
    """Resolve the pending SPAWNs whose runs DID reach the host (ss5).

    The classic outbox window: `_apply_spawn` launches and THEN records the
    outcome, so an engine that died between the two left a pending effect
    for a run that may well have started. The spool is the record (DL-93) --
    a run directory means it reached the host -- so the effect is reconciled
    from it rather than re-driven. Without this the next dispatch would drain
    a pending SPAWN into a second `mkdir()` of a directory that exists."""
    for effect in [e for e in engine.outbox.pending() if e.kind == "SPAWN"]:
        spool = candidates.get((effect.job, effect.run_number))
        if (effect.job, effect.run_number) not in candidates:
            continue
        spawned = load_json(spool / "spawn.json") if spool is not None else None
        if spawned is not None:
            # re-checked at THIS read, not only at the preflight (DL-118):
            # evidence that appeared in the window between them is held to
            # the bound identity BEFORE anything durable is recorded --
            # including present-but-idless evidence, which the strict gate
            # refuses. Without this, the applied outcome lands first and the
            # later gates refuse only after the WAL has moved.
            _refuse_identity_split(effect, spawned.get("run_id"), "the spool's spawn.json")
        run_id = (spawned or {}).get("run_id")
        detail = "reconciled from the spool: the run reached the host"
        if spawned is None and spool is not None:
            # ss11's FW rule (PR-34): a watch writes no spawn.json, and its
            # `start` line is what says the dispatch happened. Without this the
            # ladder treats the pending SPAWN as an applied-SPAWN candidate,
            # finds no spawn.json, and re-launches the watch as an untraced
            # start -- two `start` lines and a fold nothing can reproduce.
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


async def _redrive_recorded_kills(
    engine: Engine, supervised_live: dict[tuple[str, int], dict[str, Any]]
) -> None:
    """Deliver the kills the previous engine decided and did not get to
    (concurrency-model ss5; S5c).

    This closes a real leak. A kill used to be a `task.cancel()` with no id
    and no record: an engine that decided TERMINATED and died before
    cancelling left a DETACHED run whose parent is the supervisor, and
    reconciliation skipped it on the way past -- its job is already TERMINAL,
    which reads as "its completion was already replayed". Nothing looked
    again, and the process ran on orphaned.

    Re-driving is not a new licence: runner-design ss7 already permits
    exactly this side effect at resume, and only this one ("no side effects
    on resume beyond recorded kills").

    A kill whose run is NOT alive is resolved from the spool, three ways --
    which is where ss5's third state earns its keep. `status.json` saying the
    command was signalled means the kill landed; saying it exited means it
    finished first and the kill is retired, superseded by the truth. No
    status record at all and no live wrapper means nobody can say whether the
    signal landed, and `indeterminate` is the only honest answer: reporting
    it either way would invent a fact about a process nothing observed
    (E7).

    *(Amended by DL-133, at build of period-model ss3.5 -- PR-33.)* **A live
    wrapper under a TERMINAL row is re-driven regardless of the KILL
    effect's recorded state**, and regardless of whether a KILL effect
    exists at all. `_apply_kill` records `applied` when the cancellation is
    delivered and the TERM/grace/KILL ladder runs on the way out of the
    task, so an engine that dies mid-ladder leaves a live wrapper under a
    terminal row -- and re-driving only PENDING kills read that state and
    walked past it. The row being terminal is what makes the process an
    orphan: reconciliation skips it as "already replayed", and nothing else
    ever looks again."""
    assert engine.run_root is not None
    redriven: set[str] = set()
    for effect in [e for e in engine.outbox.pending() if e.kind == "KILL"]:
        listing = supervised_live.get((effect.job, effect.run_number))
        job_ir = engine.oracle.catalog.jobs.get(effect.job)
        adapter = engine.adapters.get(job_ir.job_type) if job_ir is not None else None
        if (
            job_ir is not None
            and listing is not None
            and listing.get("wrapper_alive")
            and isinstance(adapter, SupervisedCommandAdapter)
        ):
            # the adapter's own TERM/grace/KILL ladder, driven directly. Not
            # through a reattached task and a cancel: a task cancelled before
            # its first step never enters the handler that runs the ladder, so
            # that route would resolve the effect while stopping nothing.
            # `_live` is empty here anyway -- the supervisor's LIST is what
            # says this run is alive, which is why `_apply_effect`'s
            # supersession check (which reads `_live`) is not the right gate
            # at resume.
            run_id = str(listing["run_id"])
            redriven.add(run_id)
            await adapter.kill(run_id)
            engine._resolve_effect(
                EffectOutcome(
                    effect_id=effect.effect_id,
                    state="applied",
                    run_id=run_id,
                    detail="re-driven at resume: the wrapper was still alive",
                )
            )
            continue
        engine._resolve_effect(_kill_outcome_from_spool(engine.run_root, effect))
    await _redrive_orphans(engine, supervised_live, redriven)


async def _redrive_orphans(
    engine: Engine, supervised_live: dict[tuple[str, int], dict[str, Any]], redriven: set[str]
) -> None:
    """PR-33: every live wrapper whose row is TERMINAL, killed at resume --
    whatever the KILL effect says, and whether or not one exists.

    Keyed the other way round from the loop above, deliberately. That loop
    asks "which kills did the previous engine intend?"; this asks "which
    processes are alive that nothing intends to be", which is the question
    an `applied`, `retired` or absent KILL effect cannot answer. A run this
    resume already killed is skipped rather than signalled twice."""
    for (job, run_number), listing in sorted(supervised_live.items()):
        run_id = str(listing.get("run_id") or "")
        if not listing.get("wrapper_alive") or not run_id or run_id in redriven:
            continue
        row = engine.oracle.store.job.get(job)
        if row is None or row.run_number != run_number or row.status not in TERMINAL:
            continue
        job_ir = engine.oracle.catalog.jobs.get(job)
        adapter = engine.adapters.get(job_ir.job_type) if job_ir is not None else None
        if isinstance(adapter, SupervisedCommandAdapter):
            await adapter.kill(run_id)
            redriven.add(run_id)


def _kill_outcome_from_spool(run_root: Path, effect: Effect) -> EffectOutcome:
    """What the spool can say about an undelivered kill (ss5's three states).

    The record is read through `outcome_from_status`, the one mapping the
    live adapter path and reconciliation already share, rather than by
    reaching into the record here. Its `Terminated` IS "this run ended by a
    kill", which is the question -- and the first draft of this function
    asked a different one, reading `observed`, which the wrapper writes as
    FORENSICS about how the group died rather than as its verdict."""
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
            else "the run ended on its own before the kill was delivered"
        ),
    )
