"""The estate verbs: the period boundary and what lives around it
(DL-137's split).

`seal` closes the running period and commits the next one, live or
offline (period-model ss7); `audit` re-derives a closed period and writes
its attestation, `verify` validates one; `estate reclaim` moves a stale
successor claim out of the way and `estate prune` deletes what retention
allows. Registered on the app in `cli.py`, where the `estate` sub-app is
assembled.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from dsl41.ast_jil import parse
from dsl41.cli_common import (
    PERMIT_UNKNOWN,
    PROPERTIES,
    check_base_tz,
    command_outcome,
    load_catalog_and_ast_or_exit_2,
    load_tz_aliases,
    read_header_of,
    refuse,
    say_next,
)
from dsl41.ir import lower_catalog

if TYPE_CHECKING:
    from dsl41.boundary import SealRequest
    from dsl41.ir import CatalogIR
    from dsl41.period import RuntimeProfile, StagedManifest
    from dsl41.runner import Engine
    from dsl41.runner_ledger import LeaderLock
    from dsl41.seal import StagedNextPeriod


# ------------------------------------------------------- the boundary (U7)

_RUN_ROOT_OPT = typer.Option(..., "--run-root", help="The estate root (period-model ss1.1).")

_ANCHOR_OPT = typer.Option(
    None,
    "--estate-anchor",
    help="The lineage anchor directory (period-model ss1.3). Defaults to"
    " <run-root>.anchor -- a sibling of the root, never inside it, because the"
    " root is what an operator archives. A ROLLED root's anchor is the lineage's"
    " and must be named explicitly.",
)

_ACTOR_OPT = typer.Option(
    None,
    "--claimed-actor",
    help="Who is asking, for the log. A CLAIM: this tier has no authentication"
    " (control-protocol ss7 gap 2), so it is a breadcrumb, never an"
    " authorization. Defaults to <user>@<host>.",
)


def _next_profile(
    timezone: "str | None",
    timezone_map: "Path | None",
    as_machine: list[str],
    machine_policy: str,
    detached: bool,
    deadman: "float | None",
) -> "RuntimeProfile":
    """C2's `RuntimeProfile` from the `--next-*` flags (period-model ss2.1).

    Prefixed because a boundary names TWO periods and the CLI would
    otherwise read as if it were describing the one that is running. What
    it describes is the one about to open."""
    from dsl41.period import runtime_profile_from_cli

    if machine_policy not in ("strict", "local-eligible"):
        # the same guard `run`/`rehearse` apply: without it a bad flag
        # surfaces as an uncaught ValidationError and exit 1 -- documented
        # as "the estate failed while running", which it never did (DL-137)
        typer.echo(f"--machine-policy {machine_policy!r}: expected strict|local-eligible", err=True)
        raise typer.Exit(2)
    tz_aliases = load_tz_aliases(timezone_map)
    check_base_tz(timezone, tz_aliases)
    return runtime_profile_from_cli(
        timezone=timezone,
        tz_aliases=tz_aliases,
        as_machine=as_machine,
        machine_policy=machine_policy,
        detached=detached,
        deadman_s=deadman,
    )


def _stage_next(
    run_root: Path,
    files: list[Path],
    profile: "RuntimeProfile",
    permit_unknown: bool,
    properties: "list[Path] | None",
) -> "tuple[StagedNextPeriod, StagedManifest, CatalogIR]":
    """ss7's staging, both modes: the immutable bundle, then
    `staged_manifest.json` and `candidate.json` under
    `periods/.staging/<stage_digest>/`.

    Content-addressed, so a repeat is idempotent and a concurrent client
    writing the same bytes is harmless -- which is what makes it safe to do
    this against a LIVE engine's root without holding its lock."""
    from dsl41.boundary import stage_next_period, stage_period

    catalog, parsed, _ = load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)
    staged_manifest = stage_period(run_root, parsed, catalog, profile)
    staged = stage_next_period(run_root, staged_manifest=staged_manifest)
    return staged, staged_manifest, catalog


def seal(
    next_files: list[Path] = typer.Option(
        ...,
        "--next",
        help="JIL file(s) forming C2 -- the estate the NEXT period runs."
        " Repeatable; command-line order is part of source_bundle_hash.",
    ),
    run_root: Path = _RUN_ROOT_OPT,
    estate_anchor: Path = _ANCHOR_OPT,
    force_seal: bool = typer.Option(
        False,
        "--force-seal",
        help="Commit inside the closing period's retry horizon (period-model ss9)."
        " Recorded as force_seal: true in the seal and, when the gate was really"
        " engaged, with the gate's own numbers in forced_gate.",
    ),
    claimed_actor: str = _ACTOR_OPT,
    request_id: str = typer.Option(
        None,
        "--request-id",
        help="Reuse the id of an earlier attempt to RETRY it exactly. A committed"
        " boundary answers its own exact retry from the next period; anything"
        " else is a fresh request.",
    ),
    next_timezone: str = typer.Option(
        None, "--next-timezone", help="C2's base zone for schedules without a per-job one."
    ),
    next_timezone_map: Path = typer.Option(
        None, "--next-timezone-map", help="C2's vendor timezone table (SEM-35/DL-62)."
    ),
    next_as_machine: list[str] = typer.Option(
        [], "--next-as-machine", help="Machine name(s) C2 runs as (DL-52). Repeatable."
    ),
    next_machine_policy: str = typer.Option(
        "strict", "--next-machine-policy", help="C2's machine policy: strict|local-eligible."
    ),
    next_detached: bool = typer.Option(
        False, "--next-detached", help="C2 runs CMD jobs under the supervisor (ss6a Tier 1)."
    ),
    next_deadman: float = typer.Option(
        None, "--next-deadman", help="C2's supervisor deadman, seconds. Needs --next-detached."
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Close the running period and commit the next one (period-model ss7).

    Two entry modes and one body. **Live**: an engine leads `--run-root`,
    so this stages C2 and asks it over the control socket; the engine runs
    the cutoff in its single-writer loop and then exits with code 3.
    **Offline**: nothing leads the root, so this takes `leader.lock` and
    `anchor.lock`, appends a `leader` record, runs the same-root recovery
    barrier in full, and performs the boundary as that offline leader.
    Which one you get is decided by the lock, not by a flag: an engine that
    holds it is a live engine.

    Step 9 in both modes is an OPENER -- `dsl41 run --resume` on the same
    root, or `dsl41 run --open-from` into a fresh one. A transition is a
    restart, not a reload.

    Exit codes: 0 the boundary committed; 2 it did NOT commit and the period
    is still open (C1 may legitimately have advanced first -- an offline
    sealer's `leader` record and the cutoff's admitted ticks are C1
    activity, not damage); 4 the outcome is UNKNOWN, and the printed
    request_id is the only safe way to retry.
    """
    import asyncio

    from dsl41.runner_clock import EngineError
    from dsl41.runner_control import claimed_actor as default_actor
    from dsl41.runner_ledger import acquire_run_root

    if next_deadman is not None and not next_detached:
        typer.echo(
            "--next-deadman needs --next-detached: a tethered run has no supervisor", err=True
        )
        raise typer.Exit(2)
    profile = _next_profile(
        next_timezone,
        next_timezone_map,
        next_as_machine,
        next_machine_policy,
        next_detached,
        next_deadman,
    )
    actor = claimed_actor or default_actor()
    try:
        lock = acquire_run_root(run_root)
    except EngineError:
        # the lock IS the discriminator: an engine that holds it is a live
        # engine, and probing a socket would answer a different question
        # (a socket file outlives the process that made it)
        raise typer.Exit(
            _live_seal(
                run_root,
                next_files,
                profile,
                permit_unknown,
                properties,
                force_seal,
                actor,
                request_id,
            )
        ) from None
    try:
        raise typer.Exit(
            asyncio.run(
                _offline_seal(
                    run_root,
                    estate_anchor,
                    next_files,
                    profile,
                    permit_unknown,
                    properties,
                    force_seal,
                    actor,
                    request_id,
                    lock,
                )
            )
        )
    finally:
        lock.release()


def _live_seal(
    run_root: Path,
    next_files: list[Path],
    profile: "RuntimeProfile",
    permit_unknown: bool,
    properties: "list[Path] | None",
    force_seal: bool,
    actor: str,
    request_id: "str | None",
) -> int:
    """ss7 live mode: stage C2, then ask the leading engine for the
    boundary over the control socket.

    The CLI stages FIRST and names the staged bytes by `stage_digest`; the
    engine validates exactly those bytes. Two clients racing on one root
    stage under two fingerprints and the engine commits exactly the one its
    request names.

    The ANSWER is read by `cli_common.command_outcome`, the same ladder
    `sendevent` and `host` exit on: a boundary asked for over the socket is
    a mutation, and DL-92's four outcomes are the protocol's reading of one,
    not the verb's (DL-137 -- the copy this replaced classified by hand).
    What this SURFACE does with that reading is still its own, and ss7's
    table is 0/2/4: an answer it cannot classify stays `unknown`, as it was
    before the ladder was shared."""
    import uuid

    from dsl41.runner_control import ControlClientError, roundtrip

    socket_path = run_root / "control.sock"
    if not socket_path.exists():
        typer.echo(
            f"{run_root}: an engine holds leader.lock and {socket_path} is not there --"
            " a live seal is asked for over the control socket, and this root has a"
            " leader with no door (period-model ss7)",
            err=True,
        )
        return 2
    try:
        header = roundtrip(socket_path, {"cmd": "status"})
    except ControlClientError as exc:
        return refuse(exc, prefix=str(socket_path))
    parsed_header = read_header_of(header)
    if parsed_header is None:
        return 2
    baseline, epoch = parsed_header
    # `staged` is the OWNER's projection (stage_next_period's return,
    # DL-137) -- the reflection rebuild it replaces was the third spelling
    # of which fields cross from launcher-pin to client-proposal
    staged, staged_manifest, _ = _stage_next(
        run_root, next_files, profile, permit_unknown, properties
    )
    request = {
        "cmd": "seal",
        "v": 3,
        "baseline_id": baseline,
        "epoch": epoch,
        "request_id": request_id or str(uuid.uuid4()),
        "next_period": staged.model_dump(mode="json"),
        "stage_digest": staged.stage_digest,
        "force_seal": force_seal,
        "claimed_actor": actor,
    }
    return command_outcome(
        socket_path,
        request,
        on_applied=lambda: say_next(run_root, None),
        # ss7 publishes 0/2/4 for this verb and 3 means something else here
        # (a sealed ENGINE exits 3); see `command_outcome`
        rejected_as_unknown=True,
    )


async def _offline_seal(
    run_root: Path,
    estate_anchor: "Path | None",
    next_files: list[Path],
    profile: "RuntimeProfile",
    permit_unknown: bool,
    properties: "list[Path] | None",
    force_seal: bool,
    actor: str,
    request_id: "str | None",
    lock: "LeaderLock",
) -> int:
    """ss7 offline mode: no engine, so this process becomes the leader for
    exactly one boundary.

    `leader.lock` is already held (the caller took it, which is how the two
    modes are told apart). `resume_run` is the same-root recovery barrier
    in full -- it takes `anchor.lock`, appends a `leader` record at
    epoch+1, replays, reconciles and re-drives recorded kills -- and the
    boundary that follows is the SAME `submit_seal` a live engine serves.
    Two entry modes, one body; the alternative is two implementations of
    the one thing this model exists to have exactly one of.

    C1 is loaded from the ROOT's own bundle, never from the command line:
    the run root outlives the estate files it was launched from, and the
    closing period's identity is the manifest's."""
    import uuid

    from dsl41.boundary import SealRequest
    from dsl41.runner_clock import EngineError, RealClock
    from dsl41.runner_history import active_period_id
    from dsl41.period import read_period_manifest
    from dsl41.runner_startup import resume_run, wire_from_profile

    try:
        pinned = read_period_manifest(run_root, active_period_id(run_root))
    except Exception as exc:  # RunHistoryError or EngineError: both are refusals
        return refuse(exc, prefix=str(run_root))
    if pinned is None:
        typer.echo(
            f"{run_root}: no period manifest -- an offline seal reads the CLOSING"
            " period's identity from the root itself, and this root has none"
            " (period-model ss7)",
            err=True,
        )
        return 2
    staged, staged_manifest, _ = _stage_next(
        run_root, next_files, profile, permit_unknown, properties
    )
    wiring = None
    try:
        catalog = _catalog_from_root(run_root, pinned.source_bundle_hash)
        wiring = await wire_from_profile(run_root, catalog, pinned.runtime_profile)
        engine = await resume_run(
            catalog,
            run_root,
            clock=RealClock(),
            adapters=wiring.adapters,
            scheduler=wiring.scheduler,
            hold_open=True,
            supervisor=wiring.client,
            deadman_s=wiring.deadman_s,
            lock=lock,
            anchor_dir=estate_anchor,
        )
    except EngineError as exc:
        code = refuse(exc)
        if wiring is not None:
            await wiring.close()
        return code
    request = SealRequest(
        baseline_id=engine.baseline_id,
        epoch=engine.epoch,
        request_id=request_id or str(uuid.uuid4()),
        next_period=staged,
        stage_digest=staged.stage_digest,
        force_seal=force_seal,
        claimed_actor=actor,
    )
    code = await _drive_boundary(engine, request, run_root, estate_anchor)
    await wiring.close()
    return code


async def _drive_boundary(
    engine: "Engine", request: "SealRequest", run_root: Path, estate_anchor: "Path | None"
) -> int:
    """One queued boundary, driven to its outcome by this process's own
    loop -- the offline sealer's tail.

    Two things can finish first and they mean different things. The LOOP
    ending is a committed boundary (`PeriodSealed`), a fail-stop, or a
    crash. The REQUEST's future ending while the loop runs on is a refusal:
    `abort_boundary` reopened admission and the period is still open, which
    is exactly what ss7's exit code 2 is for -- so waiting on the loop
    alone would wait forever for an engine that is correctly still
    serving C1.

    The three exits are ss7's: 0 committed, 2 refused with C1 still open,
    4 an unknown outcome whose only safe retry is the printed
    `request_id`."""
    import asyncio
    import contextlib

    from datetime import datetime

    from dsl41.boundary import BoundaryFailStop, PeriodSealed
    from dsl41.runner_clock import EngineError

    # `ensure_future` over the engine's own future: it hands the same
    # object back and gives this function the `done`/`exception`/`result`
    # surface its three-way classification is written against
    future = asyncio.ensure_future(engine.submit_seal(request))
    loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    await asyncio.wait({future, loop_task}, return_when=asyncio.FIRST_COMPLETED)
    code = 2
    outcome: BaseException | None = None
    if future.done() and future.exception() is None:
        # **The FUTURE decides, not the loop.** It is the boundary's own
        # answer, and once it holds a committed boundary the boundary
        # committed -- whatever the loop does afterwards. Reading the loop
        # first would let an unrelated engine failure during teardown
        # report exit 2, which ss7 defines as "it did NOT commit and the
        # period is still open": the one lie about the estate this
        # function could tell. The loop is still awaited, because it is a
        # bounded number of turns from its own `PeriodSealed` and that
        # object is the single authority for the sentence an operator
        # reads; anything else it raises is printed as diagnostics.
        outcome = PeriodSealed(future.result())
        try:
            await loop_task
        except PeriodSealed as sealed:
            outcome = sealed
        except BaseException as raised:  # noqa: BLE001 -- diagnostics only
            typer.echo(f"the engine stopped after the boundary: {raised}", err=True)
    elif loop_task.done():
        outcome = loop_task.exception()
    else:
        outcome = future.exception()
        loop_task.cancel()
    if isinstance(outcome, PeriodSealed):
        typer.echo(str(outcome))
        say_next(run_root, estate_anchor)
        code = 0
    elif isinstance(outcome, BoundaryFailStop):
        typer.echo(str(outcome), err=True)
        code = 4
    elif outcome is not None:
        typer.echo(str(outcome), err=True)
        code = 2 if isinstance(outcome, EngineError) else 1
    else:
        typer.echo("the engine loop returned without a boundary", err=True)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await loop_task
    with contextlib.suppress(Exception):
        await engine.shutdown()
    if engine.journal is not None:
        engine.journal.close()
    return code


def _catalog_from_root(run_root: Path, source_bundle_hash: str) -> "CatalogIR":
    """C1, from the root's own immutable bundle (period-model ss7).

    Parsed under the ORIGINAL paths `sources.json` records, because
    `catalog_hash` v2 covers spans and a span names its file."""
    from dsl41.period import bundle_sources

    sources = bundle_sources(run_root, source_bundle_hash)
    return lower_catalog([parse(source.text, file=source.path) for source in sources])


def audit(
    run_root: Path = _RUN_ROOT_OPT,
    estate_anchor: Path = _ANCHOR_OPT,
    period: int = typer.Option(
        None, "--period", help="Audit exactly this period. Omit to audit every closed one."
    ),
) -> None:
    """Re-derive a closed period and write its attestation (period-model
    ss1.3, ss11).

    **Verified means re-derived, not self-consistent.** A sidecar whose
    digest matches its own canonical form proves integrity, not derivation,
    so this rebuilds the seal from the period's own evidence -- the opening
    seal, the complete ordered WAL, the immutable spool, and the C1 and C2
    manifests -- and refuses when the two disagree, naming the fields.

    **Producing an attestation and consuming one are two acts with two
    rules.** Producing N requires the PREDECESSOR attestation present and
    VERIFIED;
    period 1 is the base case. There is deliberately no "or re-derive
    everything below" alternative, because without the requirement a
    checkpoint can be emitted over an unaudited opening seal and earlier
    roots then get deleted on a chain that was never established.

    `dsl41 verify` is the other verb and is not this one: it validates an
    attestation, which is what a rolled root can do and a full audit is
    not.

    Exit 0 when every period asked for is attested, 2 on any refusal.
    """
    from dsl41.attest import Unattested, audit_period
    from dsl41.boundary import EstateAnchor, default_anchor_dir
    from dsl41.period import closed_periods
    from dsl41.runner_clock import EngineError

    periods = [period] if period is not None else closed_periods(run_root)
    if not periods:
        typer.echo(f"{run_root}: no closed period to audit", err=True)
        raise typer.Exit(2)
    # the anchor is taken by `audit_period` for the ONE write that needs it
    # and released again, so auditing a closed period while a later one is
    # live is possible: a leader holds the lineage lock for its whole life
    anchor = EstateAnchor(estate_anchor or default_anchor_dir(run_root))
    try:
        for period_id in periods:
            attestation = audit_period(run_root, period_id, anchor=anchor)
            typer.echo(
                f"period {period_id} attested: {attestation.digest}"
                f" (seal {attestation.seal_digest}, chain through"
                f" {attestation.chain_through_period})"
            )
    except Unattested as exc:
        # the checkpoint IS written; only the registry row is not, and the
        # row is bookkeeping. Loud on stderr, and not a failure
        typer.echo(str(exc), err=True)
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc


def verify(
    run_root: Path = _RUN_ROOT_OPT,
    period: int = typer.Option(
        None, "--period", help="Verify this period's attestation. Omit for the newest one."
    ),
) -> None:
    """Validate an attestation: its own digest, its binding to the seal it
    names, and its place in the chain (period-model ss1.3).

    It accepts the attestation ALONE, deliberately. The producing `audit`
    already established the induction, and a physical roll imports only the
    current seal and its attestation -- a consumer that re-walked the chain
    would make a second roll impossible. So a root that imported seal 2 and
    attestation 2 while its predecessors are gone verifies the chain below
    seal 2, because attestation 2 proves it.

    Exit 0 when it verifies, 2 otherwise.
    """
    from dsl41.attest import verify_attestation
    from dsl41.period import attestation_path, attestation_periods
    from dsl41.runner_clock import EngineError

    if period is None:
        held = attestation_periods(run_root)
        if not held:
            typer.echo(f"{run_root}: no attestation to verify", err=True)
            raise typer.Exit(2)
        period = held[-1]
    try:
        attestation = verify_attestation(run_root, period)
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc
    typer.echo(
        f"{attestation_path(run_root, period)} verifies: seal {attestation.seal_digest},"
        f" chain through period {attestation.chain_through_period},"
        f" produced by dsl41 {attestation.dsl41_version}"
    )


def estate_reclaim(
    estate_anchor: Path = typer.Option(
        ..., "--estate-anchor", help="The lineage anchor directory (period-model ss1.3)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Required. This is the one operation that can fork a lineage."
    ),
    claimed_actor: str = _ACTOR_OPT,
) -> None:
    """Break-glass: move a successor claim out of the way (period-model
    ss1.3).

    **A stale claim is break-glass, not garbage.** A `claimed` head whose
    target root is unreachable cannot be told from one whose target is
    merely paused, and nothing here decides that -- you do. If the claimant
    is alive, this forks the lineage: two roots then open the same period,
    allocate the same indices and run the same `(job, run_number)` twice,
    which is the safety property the whole fence exists to hold. Prove the
    claimant is gone before you run it.

    It is recorded in the anchor and again in the next `segment` record's
    `reclaimed` field with the actor who claimed to authorize it -- loud,
    durable and attributable.

    Exit 0 when the head moved, 2 otherwise.
    """
    from dsl41.boundary import EstateAnchor
    from dsl41.runner_clock import EngineError
    from dsl41.runner_control import claimed_actor as default_actor

    if not force:
        typer.echo(
            "refusing without --force: reclaiming a live claimant's head forks the"
            " lineage, and this verb exists for the case where you have PROVED it is"
            " gone (period-model ss1.3)",
            err=True,
        )
        raise typer.Exit(2)
    anchor = EstateAnchor(estate_anchor)
    try:
        anchor.acquire()
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc
    try:
        stored = anchor.require()
        _, moved = anchor.reclaim(
            estate_id=stored.estate_id, claimed_actor=claimed_actor or default_actor()
        )
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc
    finally:
        anchor.release()
    typer.echo(
        f"reclaimed claim {moved.claim_id} from {moved.target_root}: period"
        f" {moved.next_period} may be opened again, and the next opening `segment`"
        f" will record that {moved.claimed_actor} said so"
    )


def estate_prune(
    run_root: Path = _RUN_ROOT_OPT,
    estate_anchor: Path = _ANCHOR_OPT,
    dry_run: bool = typer.Option(False, "--dry-run", help="List every verdict and delete nothing."),
    tombstones: bool = typer.Option(
        False,
        "--tombstones",
        help="Remove SPAWN tombstones -- run directory, `.by_run_id` entry and"
        " default logs -- whose period is attested and whose run is terminal.",
    ),
    quarantine: bool = typer.Option(
        False,
        "--quarantine",
        help="Remove quarantined candidates: superseded staged periods no recovery references.",
    ),
    older_than_days: float = typer.Option(
        None,
        "--older-than-days",
        help="Keep any run spool touched more recently than this. Your policy, not the model's.",
    ),
    keep_runs: int = typer.Option(
        0,
        "--keep-runs",
        help="Keep the N newest run spools OF EACH JOB, whatever else says."
        " Per job, because `run_number` is per job.",
    ),
) -> None:
    """Delete what retention allows, and report what it does not
    (period-model ss11a, ss12; PR-36b, PR-36c).

    **Retention policy is yours; the floors are the model's.** Which
    periods, spools and tombstones an estate keeps is a business decision,
    so the flags above are how you state it. What may never go is
    everything reachable from the lineage head -- the sentinel, the anchor
    and any live claim, the sidecars this period opened from and will close
    with, the current and committed-next manifests, an uncommitted
    candidate's two files, their bundles, the latest attestation, and the
    WAL and spool of any unattested period. This verb cannot reach them.

    Three verdicts are reported. `floored` is refused by the model.
    `held` has been released by the head moving on and is kept anyway,
    because PR-Q3/E20 -- may a seal-only archive stand in for pruned
    inputs? -- is open. `prunable` is licensed by name: a tombstone whose
    period is attested and whose run has ended, and a quarantined
    candidate.

    Pruning a tombstone is not reversible and it is not free: that period
    can no longer be re-derived from its own evidence, and its attestation
    becomes the proof that stands for it. Attest first, then prune.

    Exit 0 when every selected artifact was removed (or listed, under
    `--dry-run`), 2 on a refusal and 2 when the filesystem refused a
    removal -- and then the report says which ones went and which did not.
    """
    from dsl41.retention import CLASSES, Artifact, plan_retention, prune
    from dsl41.runner_clock import EngineError

    classes = [name for name, on in (("tombstones", tombstones), ("quarantine", quarantine)) if on]
    if not classes and dry_run:
        # a listing with no class named is a survey: it shows every
        # licensed deletion, so the operator can pick from what is there
        classes = sorted(CLASSES)
    if not classes and not dry_run:
        typer.echo(
            "nothing selected: name at least one class (--tombstones, --quarantine)"
            " or ask for --dry-run. A prune verb with a default set would be a"
            " retention policy, and that is the operator's (period-model ss12)",
            err=True,
        )
        raise typer.Exit(2)
    try:
        plan = plan_retention(run_root, anchor_dir=estate_anchor)
        report = prune(
            plan,
            classes=classes,
            dry_run=dry_run,
            older_than_days=older_than_days,
            keep_runs=keep_runs,
        )
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc

    def _lines(title: str, items: tuple[Artifact, ...]) -> None:
        typer.echo(f"{title} ({len(items)}):")
        for item in items:
            typer.echo(f"  {item.render()}")

    verb = "would remove" if report.dry_run else "removed"
    _lines(verb, report.removed)
    _lines("prunable, outside the flags given", report.kept)
    _lines("held (floor lifted, PR-Q3/E20 open)", report.held)
    _lines("floored (the model refuses)", report.floored)
    if report.failed:
        typer.echo(f"the filesystem refused ({len(report.failed)}):", err=True)
        for item, reason in report.failed:
            typer.echo(f"  {item.path}: {reason}", err=True)
    typer.echo(
        f"{verb} {len(report.removed)} artifact(s), {report.bytes_removed} byte(s);"
        f" {len(report.floored)} floored, {len(report.held)} held"
        f" -- estate {plan.estate_id}, period {plan.current_period},"
        f" attested {sorted(plan.attested) or 'none'}"
    )
    if report.failed:
        raise typer.Exit(2)
