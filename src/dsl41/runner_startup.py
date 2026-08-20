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
from dsl41.runner_journal import (
    Journal,
    last_journal_at,
    baseline_id,
    catalog_hash,
    read_journal,
    replay_inputs,
)
from dsl41.runner_ledger import (
    LeaderLock,
    acquire_run_root,
    check_leader_eligibility,
    next_epoch,
)
from dsl41.runner_scheduler import Scheduler


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
) -> Engine:
    """Create the run-root layout (journal.jsonl, runs/, logs/) and an
    Engine wired to it. Refuses a run_root that already holds a journal --
    that is what --resume is for (no silent re-baselining).

    Leadership (S6a) is acquired BEFORE that refusal, not after: the
    refusal reads the estate's state, and one leader per run root is the
    rule under which any such read is meaningful. Genesis is epoch 1. A
    caller that already holds the run root passes its `lock` -- `dsl41 run`
    takes leadership earlier still, before it starts a supervisor. Either
    way a failure here releases it, because a caller that got this far and
    was refused is on its way out."""
    run_root.mkdir(parents=True, exist_ok=True)
    # the run root holds the journal (global values, every control input),
    # job output, and data -- owner-only, loudly, not umask-hopefully
    os.chmod(run_root, 0o700)
    lock = lock or acquire_run_root(run_root)
    journal_path = run_root / "journal.jsonl"
    if journal_path.exists():
        lock.release()
        raise EngineError(
            f"{journal_path} already exists: resume it (resume_run) or pick a fresh run root"
        )
    (run_root / "runs").mkdir(exist_ok=True)
    (run_root / "logs").mkdir(exist_ok=True)
    at = clock.now()
    journal = Journal.create(
        journal_path,
        catalog=catalog,
        clock_domain="virtual" if clock.virtual else "real",
        started_at=at,
        lock=lock,
    )
    epoch = next_epoch([])  # the first term over a log that has none
    journal.leader(epoch=epoch, at=at)
    lock.note(epoch=epoch, at=at)
    fsync_dir(run_root)  # the journal's directory entry is a record too
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
    )


async def resume_run(
    catalog: CatalogIR,
    run_root: Path,
    *,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    scheduler: Scheduler | None = None,
    hold_open: bool = False,
    settle_seconds: float = 5.0,
    grace_seconds: float = 10.0,
    supervisor: SupervisorClient | None = None,
    deadman_s: float | None = None,
    lock: LeaderLock | None = None,
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
    try:
        engine = await _resume_under_lock(
            catalog,
            run_root,
            lock,
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
        lock.release()  # a refused resume holds nothing: the next engine may lead
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
    settle_seconds: float,
    grace_seconds: float,
    supervisor: SupervisorClient | None,
    deadman_s: float | None,
) -> Engine:
    """The ss7 resume ladder proper, with leadership already held (S6a).
    Split from `resume_run` so the acquire/release pairing is one readable
    block rather than a `finally` wrapped around a hundred lines."""
    records = read_journal(run_root / "journal.jsonl")
    header = records[0]
    check_leader_eligibility(header, expected_catalog_hash=catalog_hash(catalog))
    domain = "virtual" if clock.virtual else "real"
    if header.get("clock_domain") != domain:
        raise EngineError(
            f"clock-domain mismatch: journal is {header.get('clock_domain')!r},"
            f" resume clock is {domain!r}"
        )
    last_at = last_journal_at(records)
    if not clock.virtual and last_at > clock.now():
        raise EngineError(
            f"journal is from the future ({last_at.isoformat()} > now): the machine"
            " clock moved backwards; refusing to feed non-decreasing time backwards"
        )
    journal = Journal(
        run_root / "journal.jsonl",
        fsync_each=not clock.virtual,
        baseline_id=baseline_id(records),
        lock=lock,
    )
    # the term is allocated by being appended (ss1), before the first input
    # this incarnation admits, so every record after it names its author
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
    )
    replay = replay_inputs(engine.oracle, records)
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
        scheduler.reset(last_at, inclusive=True)
        sweep_upto = max(clock.now(), last_at)  # virtual resume: now < last_at
        for tick_ev in scheduler.pop_due(sweep_upto):
            if (tick_ev.job(), tick_ev.at.isoformat()) in replayed_ticks:
                continue  # replay already fed this tick
            reason = "scheduler tick missed while the engine was down; not fired late"
            engine.drops.append((tick_ev, reason))  # PENDING: E9
            journal.drop(tick_ev, reason)
    await _reconcile(
        engine,
        records,
        last_at,
        settle_seconds=settle_seconds,
        grace_seconds=grace_seconds,
        supervisor=supervisor,
    )
    return engine


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
            job, dot, num = entry.name.rpartition(".")
            if entry.is_dir() and dot and num.isdigit():
                candidates.setdefault((job, int(num)), entry)
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
        if watch.last_at is not None:
            extras["ended_at"] = watch.last_at.isoformat()
        _inject_completion(
            engine, job, run_number, extras, at=watch.last_at or last_at, last_at=last_at
        )
        return
    adapter = engine.adapters.get("FW")
    if adapter is None:
        raise EngineError(  # refuse loudly: never leave it hanging
            f"incomplete FW run {job}.{run_number}: no FW adapter registered to re-dispatch it"
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
                raise EngineError(
                    f"incomplete FW run {job}.{rt.run_number}: no FW adapter registered"
                    " to re-dispatch it"
                )
            bound = _spawn_effect_for(engine, job, rt.run_number)
            engine._launch(job_ir, rt.run_number, adapter, run_id=bound.run_id if bound else None)
            continue
        if job_ir.job_type not in engine.adapters:
            continue  # no dispatch row live either: parity with the running engine
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
        # to happen -- a journal written before the outbox existed (S5c), or
        # an effect already resolved whose spool has since gone -- and either
        # no supervisor path or no bound identity to replay against
        # (pre-DL-118). That is the case runner-design ss7 was reasoning
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
    (E7)."""
    assert engine.run_root is not None
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
