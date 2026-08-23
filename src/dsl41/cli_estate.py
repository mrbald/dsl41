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

from dsl41.cli_common import (
    PERMIT_UNKNOWN,
    PROPERTIES,
    check_base_tz,
    command_outcome,
    load_catalog_and_ast_or_exit_2,
    load_tz_aliases,
    read_header_of,
    refuse,
    resume_target_period,
    say_next,
    walk_estate_or_exit_2,
)
from dsl41.period import ARCHIVE_CLASS

if TYPE_CHECKING:
    from dsl41.boundary import EstateAnchor, EstateWalk, SealRequest
    from dsl41.attest import Attestation
    from dsl41.retention import Artifact, PruneReport, RetentionPlan
    from dsl41.ir import CatalogIR
    from dsl41.period import RuntimeProfile, StagedManifest
    from dsl41.runner import Engine
    from dsl41.runner_ledger import LeaderLock
    from dsl41.seal import Seal, StagedNextPeriod


# ------------------------------------------------------- the boundary (U7)

_RUN_ROOT_OPT = typer.Option(..., "--run-root", help="The estate root (period-model ss1.1).")

#: the same option where the verb ALSO has an estate-wide mode: omitting it
#: and naming the anchor alone is how a caller points at the lineage rather
#: than at one of its roots (PR-02f)
_ESTATE_ROOT_OPT = typer.Option(
    None,
    "--run-root",
    help="The estate root (period-model ss1.1). Omit it and name --estate-anchor"
    " alone to work ESTATE-WIDE: every root the registry names, in period order.",
)

_ANCHOR_OPT = typer.Option(
    None,
    "--estate-anchor",
    help="The lineage anchor directory (period-model ss1.3). Defaults to"
    " <run-root>.anchor -- a sibling of the root, never inside it, because the"
    " root is what an operator archives. A ROLLED root's anchor is the lineage's"
    " and must be named explicitly. Named ALONE, with no --run-root, it is how"
    " this verb addresses the whole estate.",
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
    it describes is the one about to open.

    ONE gate for every `--next-*` flag (DL-145). The bounds are
    `RuntimeProfile`'s -- the machine-policy literal and the positive
    deadman -- and this catches the model's refusal instead of hand-copying
    it: the copy that stood here checked the policy and not the deadman, so
    `--next-deadman 0` was an uncaught ValidationError and exit 1 while
    `run --deadman 0` was a clean exit 2. The one rule the model cannot
    hold is the PAIRING below, which is about two flags and not about a
    field."""
    from pydantic import ValidationError

    from dsl41.period import runtime_profile_from_cli

    if deadman is not None and not detached:
        # loud, not silent: without a supervisor there is nothing to hold
        # the lifelines, so nothing a deadman could bound
        # (concurrency-model ss8). `run` says the same of its own flags.
        typer.echo(
            "--next-deadman needs --next-detached: a tethered run has no supervisor", err=True
        )
        raise typer.Exit(2)
    tz_aliases = load_tz_aliases(timezone_map)
    check_base_tz(timezone, tz_aliases)
    try:
        return runtime_profile_from_cli(
            timezone=timezone,
            tz_aliases=tz_aliases,
            as_machine=as_machine,
            machine_policy=machine_policy,
            detached=detached,
            deadman_s=deadman,
        )
    except ValidationError as exc:
        raise typer.Exit(refuse(exc, prefix="the next period's runtime profile")) from None


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
                estate_anchor,
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


def _committed_boundary(run_root: Path, request_id: "str | None") -> "Seal | None":
    """The committed boundary `--request-id` names, if this root holds one.

    ss2.2 promises that an exact retry of a committed boundary is answered
    from the next period, and the engine keys that answer on the request
    FINGERPRINT -- which covers the `baseline_id` and the `epoch` the
    original attempt carried. A retry composed from TODAY's header carries
    the NEW period's baseline and term, so it names a different command and
    is refused as a collision: the promised route was unreachable from this
    CLI until DL-151. The seal is where the boundary-time envelope survives.

    Exactly one seal back, like the engine's own route (PR-30e). A root this
    cannot read offers no retry and the ordinary path answers -- the CLI is
    composing a request here, not auditing a lineage."""
    if request_id is None:
        return None
    from dsl41.boundary import read_seal
    from dsl41.period import sealed_periods
    from dsl41.runner_clock import EngineError

    try:
        periods = sealed_periods(run_root)
        if not periods:
            return None
        seal = read_seal(run_root, periods[-1])
    except (OSError, ValueError, KeyError, EngineError):
        return None
    return seal if seal.boundary_request.request_id == request_id else None


def _live_seal(
    run_root: Path,
    estate_anchor: "Path | None",
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
    retry = _committed_boundary(run_root, request_id)
    if retry is not None:
        # ss2.2's retry route: the envelope the ORIGINAL attempt carried,
        # which is the only one the engine's stored decision answers to
        baseline, epoch = retry.baseline_id, retry.epoch
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
        # the anchor the caller named rides into the printed resume command
        # (DL-145): a ROLLED root's anchor is the LINEAGE's and must be
        # spelled explicitly, so a hard-coded None handed the operator a
        # command that opens the wrong lineage -- and the offline sealer,
        # which does pass it, printed the right one for the same estate
        on_applied=lambda: say_next(run_root, estate_anchor),
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

    from datetime import UTC, datetime

    from dsl41.boundary import SealRequest, load_bundle_catalog
    from dsl41.runner_clock import EngineError, RealClock
    from dsl41.period import read_period_manifest
    from dsl41.runner_startup import resume_run, wire_from_profile

    try:
        # the period this sealer will CLOSE, which on a root with a
        # committed boundary is the one the resume below opens (DL-151):
        # reading the newest segment's here loaded C1's bundle over a root
        # whose next segment runs C2, and the resume refused on a catalog
        # hash the operator never named
        pinned = read_period_manifest(run_root, resume_target_period(run_root))
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
        # the OWNER's loader (DL-145): the copy this replaced dropped the
        # JilParseError/LoweringError -> EngineError wrap, so a bundle
        # that no longer parses left this `except EngineError` untouched
        # and the verb exited 1 on a traceback.
        #
        # `permit_unknown` for `_period_catalog`'s reason: these are the
        # exact bytes the CLOSING period ran, the gate that decided
        # whether an unknown attribute was acceptable ran once at launch,
        # and re-asking it here would make `dsl41 seal` refuse a root
        # `dsl41 run` is serving.
        catalog = load_bundle_catalog(
            run_root, pinned.source_bundle_hash, permit_unknown=True
        )
        wiring = await wire_from_profile(
            run_root,
            catalog,
            pinned.runtime_profile,
            start=datetime.now(UTC).replace(tzinfo=None),
        )
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
    retry = _committed_boundary(run_root, request_id)
    request = SealRequest(
        # ss2.2's retry route: a retry carries the envelope its ORIGINAL
        # attempt carried, because the stored decision is fingerprinted over
        # it. Composing from the engine's CURRENT header -- which the resume
        # above has just moved on to the next period -- names a different
        # command, and offline that does not merely fail to deduplicate:
        # nothing between here and the cutoff would recognise the retry, so
        # it would close a SECOND period (DL-151).
        baseline_id=retry.baseline_id if retry is not None else engine.baseline_id,
        epoch=retry.epoch if retry is not None else engine.epoch,
        request_id=request_id or str(uuid.uuid4()),
        next_period=staged,
        stage_digest=staged.stage_digest,
        force_seal=force_seal,
        claimed_actor=actor,
    )
    answered = _answer_from_committed(retry, request, run_root, estate_anchor)
    if answered is not None:
        await _close_engine(engine)
        await wiring.close()
        return answered
    code = await _drive_boundary(engine, request, run_root, estate_anchor)
    await wiring.close()
    return code


def _answer_from_committed(
    seal: "Seal | None",
    request: "SealRequest",
    run_root: Path,
    estate_anchor: "Path | None",
) -> int | None:
    """ss2.2's retry route offline: answer from the boundary that already
    committed, or None when there is nothing to answer from.

    The live path gets this from the engine of period N+1, which keeps the
    `seal` record it opened from (`ControlServer._committed_seal`). An
    offline sealer submits to its own engine and passes no such door, so the
    same rule is applied here to the same evidence -- the sidecar -- with
    the same two outcomes: an EXACT retry is the original answer, and the
    same id under a different envelope is a collision, because force is an
    authorization and the actor is attribution and neither may be swapped
    under a retry (PR-30c, PR-30e)."""
    if seal is None:
        return None
    if seal.request_fingerprint != request.fingerprint:
        typer.echo(
            f"request_id {request.request_id} already named the boundary that closed"
            f" period {seal.period_id} under a different envelope: force is an"
            " authorization and the actor is attribution, and neither may be swapped"
            " under a retry (period-model ss2.2, PR-30c)",
            err=True,
        )
        return 2
    typer.echo(
        f"period {seal.period_id} was already closed by request_id"
        f" {request.request_id}: seal {seal.digest}. No second boundary."
        f" This root is at period {seal.next_period.period_id}, which the"
        " recovery this command ran has opened"
    )
    say_next(run_root, estate_anchor)
    return 0


async def _close_engine(engine: "Engine") -> None:
    """Give back what an offline sealer took, whatever became of the
    boundary: the engine's loop and its journal. One spelling, so an answer
    that returns early cannot leave a live engine behind."""
    import contextlib

    with contextlib.suppress(Exception):
        await engine.shutdown()
    if engine.journal is not None:
        engine.journal.close()


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
        # ss7 publishes 0/2/4 for this verb and nothing else (DL-145): the
        # boundary did NOT commit and C1 is still open, whatever kind of
        # exception said so. The exit-1 fork this replaced left the
        # published table for anything that was not an `EngineError` and
        # reported "the estate failed while running" for a period that is
        # still serving.
        code = refuse(outcome)
    else:
        typer.echo("the engine loop returned without a boundary", err=True)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await loop_task
    await _close_engine(engine)
    return code


def audit(
    run_root: Path = _ESTATE_ROOT_OPT,
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

    **An ARCHIVED period is reported at the other tier, by name (DL-144).**
    Its inputs were deleted under `estate prune --archive-inputs`, so there
    is nothing to re-derive from and the checkpoint is what stands for it.
    This verb verifies that checkpoint and says **attestation-verified**,
    never the word it uses for a period it re-derived: two proofs of two
    strengths do not share a sentence.

    **Pointed at the ESTATE it audits every root.** With `--estate-anchor`
    and no `--run-root` it takes its periods from ss1.3's archive registry
    -- every period, in period order, each re-derived in the root that
    holds it -- so a lineage that has rolled is audited as one estate and
    period 1 is found without knowing which root it went to (PR-02f). A
    root the registry names and the disk does not refuses by name; nothing
    is skipped quietly.

    Exit 0 when every period asked for is attested, 2 on any refusal.
    """
    from dsl41.boundary import EstateAnchor, default_anchor_dir

    if run_root is None and estate_anchor is None:
        typer.echo(
            "name a --run-root, or --estate-anchor alone to audit every root the"
            " registry names (period-model ss1.3, PR-02f)",
            err=True,
        )
        raise typer.Exit(2)
    # the anchor below is taken by `audit_period` for the ONE write that
    # needs it and released again, so auditing a closed period while a later
    # one is live is possible: a leader holds the lineage lock for its life
    skipped: list[str] = []
    if run_root is None:
        walk = walk_estate_or_exit_2(estate_anchor)
        anchor = EstateAnchor(estate_anchor)
        targets, skipped = _closed_across_roots(walk, period)
        where = f"{walk.anchor_dir} (estate {walk.estate_id})"
    else:
        anchor = EstateAnchor(estate_anchor or default_anchor_dir(run_root))
        # ARCHIVED periods are named too: `closed_periods` asks for a
        # sidecar AND the segment that re-derives it, which an archived
        # period no longer has. Dropping it here would answer with a
        # smaller estate than the one on disk (DL-144)
        chosen = [period] if period is not None else _root_periods(run_root)
        targets = [(period_id, run_root) for period_id in chosen]
        where = str(run_root)
    if not targets:
        for line in skipped:
            typer.echo(line)
        typer.echo(f"{where}: no closed period to audit", err=True)
        raise typer.Exit(2)
    try:
        _attest_each(targets, anchor, name_root=run_root is None)
    finally:
        # in a `finally` because a refusal partway through the walk is
        # exactly when "and here is what was not audited" is worth having
        for line in skipped:
            typer.echo(line)


def _root_periods(run_root: Path) -> list[int]:
    """Which periods a SINGLE-ROOT audit is about.

    Three sets, because "closed" alone answers with a smaller estate than
    the one on disk. `closed_periods` wants a sidecar and the segment that
    re-derives it; an ARCHIVED period has the sidecar and no segment on
    purpose; and a period this root WROTE whose segment is gone with no
    receipt is loss, which has to be met and refused rather than dropped
    from the list (DL-144)."""
    from dsl41.period import archived_periods, closed_periods, sealed_periods, wrote_period

    # the bound comes from the SIDECARS this root holds, never from
    # `closed_periods`: that list is empty exactly when every closed period
    # has lost its segment, so a range taken from it would collapse
    # precisely in the case this set exists to catch (DL-144 review)
    lost = {period_id for period_id in sealed_periods(run_root) if wrote_period(run_root, period_id)}
    return sorted(set(closed_periods(run_root)) | set(archived_periods(run_root)) | lost)


def _closed_across_roots(
    walk: "EstateWalk", period: "int | None"
) -> tuple[list[tuple[int, Path]], list[str]]:
    """The registry's periods that are CLOSED, each with its own root --
    and a line for each period that is not.

    An OPEN period is not audit's to re-derive and never was: the
    single-root verb reads `closed_periods` for the same reason. But a
    total that dropped one without a word would leave an operator counting
    periods and finding one missing, so what was not audited is reported
    with what was. The lines come back rather than being printed here, so
    they land AFTER the attestations instead of ahead of them."""
    from dsl41.period import closed_periods

    if period is not None and not any(entry.period_id == period for entry in walk.periods):
        why = (
            "has a registry row whose first segment is not durable yet, and every"
            " cross-period reader ignores a row until it is"
            if period in walk.provisional
            else "is in no registry row of this lineage"
        )
        typer.echo(f"{walk.anchor_dir}: period {period} {why} (period-model ss1.3)", err=True)
        raise typer.Exit(2)
    targets: list[tuple[int, Path]] = []
    skipped: list[str] = []
    for entry in walk.periods:
        if period is not None and entry.period_id != period:
            continue
        # an ARCHIVED period is closed and is not re-derivable, so it is a
        # target rather than a skip: `_attest_each` verifies its checkpoint
        # and reports the attestation-verified tier (DL-144)
        if entry.archived is not None or entry.period_id in closed_periods(entry.root):
            targets.append((entry.period_id, entry.root))
        else:
            skipped.append(
                f"period {entry.period_id} in {entry.root}: not closed, nothing to audit"
            )
    return targets, skipped


def _attest_each(
    targets: list[tuple[int, Path]], anchor: "EstateAnchor", *, name_root: bool
) -> None:
    """Re-derive each (period, root) in turn and report it.

    ONE loop for both modes: the estate-wide read differs from the
    single-root one in where its pairs come from and in nothing else, and
    a second copy of this would be the second place the `Unattested`
    bookkeeping case has to be remembered."""
    from dsl41.attest import (
        ATTESTATION_VERIFIED,
        DERIVATION_VERIFIED,
        Unattested,
        audit_period,
        verified_tier,
    )
    from dsl41.runner_clock import EngineError

    outstanding = 0
    try:
        for period_id, root in targets:
            at = f" in {root}" if name_root else ""
            tier = verified_tier(root, period_id)
            try:
                # SEAL-ONLY or not, this is the same call: `audit_period`
                # re-derives when the inputs are there and consumes the
                # stored checkpoint when they are not (PR-02e), and only
                # ONE of the two paths may report itself as a derivation.
                # Skipping it for an archived period would also skip the
                # `attested` row a busy lineage lock left unflipped, and
                # "the audit is idempotent and finishes the row" would
                # stop being true for exactly those periods
                attestation = audit_period(root, period_id, anchor=anchor)
            except Unattested as exc:
                # the checkpoint IS written and durable; only the registry
                # row is not, and the row is bookkeeping. `Unattested` means
                # the lineage lock was busy, which is the ORDINARY state of
                # an estate with a live engine in it -- so it is this
                # period's note and never the walk's reason to stop. It used
                # to end the loop, which on an estate-wide audit meant one
                # busy lock left every later period unaudited under exit 0
                typer.echo(str(exc), err=True)
                outstanding += 1
                continue
            typer.echo(
                _archived_line(root, period_id, at, attestation)
                if tier == ATTESTATION_VERIFIED
                else f"period {period_id}{at} attested, {DERIVATION_VERIFIED}:"
                f" {attestation.digest} (seal {attestation.seal_digest}, chain through"
                f" {attestation.chain_through_period})"
            )
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc
    if outstanding:
        typer.echo(
            f"{outstanding} checkpoint(s) are durable with the registry row"
            " outstanding: re-run this when the lineage lock is free -- the audit is"
            " idempotent and finishes the row (period-model ss1.3)",
            err=True,
        )


def _archived_line(root: Path, period_id: int, at: str, attestation: "Attestation") -> str:
    """One archived period, verified and reported at ss11's OTHER tier
    (DL-144).

    The wording shares no phrase with the derivation-verified line beside
    it. An operator scanning a hundred rows must be able to see which
    periods this estate can still re-derive and which ones it has traded
    for a checkpoint, and two lines that differed by one adjective would
    not carry that."""
    from dsl41.attest import ATTESTATION_VERIFIED
    from dsl41.period import archive_receipt_path

    return (
        f"period {period_id}{at} inputs archived, {ATTESTATION_VERIFIED}:"
        f" {attestation.digest} (seal {attestation.seal_digest}, chain through"
        f" {attestation.chain_through_period}, receipt"
        f" {archive_receipt_path(root, period_id).name}) -- not re-derivable"
    )


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
    run_root: Path = _ESTATE_ROOT_OPT,
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
    archive_inputs: bool = typer.Option(
        False,
        f"--{ARCHIVE_CLASS}",
        help="ARCHIVE a covered period: write its receipt, then delete its WAL and"
        " its committed candidate files. IRREVERSIBLE -- the period drops to the"
        " attestation-verified tier and can never be re-derived. Prune its"
        " tombstones first.",
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
        " Per job, because `run_number` is per job -- and per ROOT in the"
        " estate-wide mode, which keeps more than asked and never less.",
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

    The report has six rows and each is a different fact. **removed** (or
    **would remove**, under `--dry-run`) is what went. **prunable, outside
    the flags given** is licensed by name -- a tombstone whose period is
    attested and whose run has ended, a quarantined candidate, the INPUTS
    of a period the archive covers -- and was not asked for. **held** has
    been released by the head moving on and is kept anyway, because no
    retention class licenses it; the row says which dependency is in the
    way. **floored** is refused by the model and this verb cannot reach
    it. **the archive refused** is selected and NOT licensed: nothing was
    attempted and nothing is broken, and the reason names the order to
    follow. **the filesystem refused** is selected, licensed, and refused
    by the operating system -- a partial sweep, reported as one.

    **`--archive-inputs` is the one irreversible verdict (DL-144).** It
    answers PR-Q3 -- may a seal-only archive stand in for pruned inputs? --
    with yes, conditionally, by explicit policy. A period is archived only
    when it is attested, a LATER chain checkpoint covers it, its spool is
    already pruned, and every older period this root retains is archived.
    The receipt (`seals/<period>.archive.json`) is written before the first
    deletion and is what tells every reader afterwards that the absence is
    an archive and not a loss; it, the sidecar and the attestation may
    never be pruned. After it, the period reads at the
    **attestation-verified** tier and can never be re-derived. Restoring
    the files does not undo it.

    Pruning a tombstone is not reversible and it is not free: that period
    can no longer be re-derived from its own evidence, and its attestation
    becomes the proof that stands for it. Attest first, then prune.

    **Pointed at the ESTATE it plans every root.** With `--estate-anchor`
    and no `--run-root` it takes its roots from ss1.3's archive registry,
    in period order, and reports ONE result (PR-02f). Each root is still
    planned on its own: the floors, the refusals and the descriptor the
    removal walks are per root, because a plan is bound to the (st_dev,
    st_ino) of the root it was computed over and one plan spanning two
    roots could not hold that binding. `--keep-runs` is per job and per
    ROOT for the same reason, which keeps more than asked and never less.

    Exit 0 when every selected artifact was removed (or listed, under
    `--dry-run`), 2 on a refusal and 2 when the filesystem refused a
    removal -- and then the report says which ones went and which did not.
    """
    from dsl41.retention import CLASSES, plan_retention, prune
    from dsl41.runner_clock import EngineError

    if run_root is None and estate_anchor is None:
        typer.echo(
            "name a --run-root, or --estate-anchor alone to prune every root the"
            " registry names (period-model ss1.3, PR-02f)",
            err=True,
        )
        raise typer.Exit(2)
    classes = [
        name
        for name, on in (
            ("tombstones", tombstones),
            ("quarantine", quarantine),
            (ARCHIVE_CLASS, archive_inputs),
        )
        if on
    ]
    if not classes and dry_run:
        # a listing with no class named is a survey: it shows every
        # licensed deletion, so the operator can pick from what is there
        classes = sorted(CLASSES)
    if not classes and not dry_run:
        typer.echo(
            "nothing selected: name at least one class (--tombstones, --quarantine,"
            f" --{ARCHIVE_CLASS}) or ask for --dry-run. A prune verb with a default"
            " set would be a retention policy, and that is the operator's"
            " (period-model ss12)",
            err=True,
        )
        raise typer.Exit(2)
    if run_root is None:
        roots = walk_estate_or_exit_2(estate_anchor).roots()
    else:
        roots = (run_root,)
    done: list[tuple[RetentionPlan, PruneReport]] = []
    stopped: str | None = None
    for root in roots:
        # the try is INSIDE the loop, so a refusal on the third root still
        # reports what the first two deleted. Deletion is irreversible and
        # this verb's whole promise is that the report says which artifacts
        # went and which did not -- a refusal that discarded that would be
        # the one moment the promise is worth something
        try:
            plan = plan_retention(root, anchor_dir=estate_anchor)
            report = prune(
                plan,
                classes=classes,
                dry_run=dry_run,
                older_than_days=older_than_days,
                keep_runs=keep_runs,
            )
        except EngineError as exc:
            stopped = str(exc)
            break
        done.append((plan, report))
    if not done:
        # nothing was planned, so there is no report to keep: the refusal
        # alone, exactly as a single root has always answered it
        if stopped is None:
            # `roots` is never empty -- the walk refuses a registry with no
            # durable period, and the single-root form has its one root --
            # so an empty `done` is a break, and a break sets `stopped`
            raise AssertionError("a prune that planned no root and refused nothing")
        raise typer.Exit(refuse(stopped))
    failed = _print_prune(done, dry_run=dry_run, estate_wide=run_root is None, stopped=stopped)
    if failed or stopped is not None:
        raise typer.Exit(2)


def _print_prune(
    done: "list[tuple[RetentionPlan, PruneReport]]",
    *,
    dry_run: bool,
    estate_wide: bool,
    stopped: str | None,
) -> bool:
    """One report over every root that was planned. Answers whether the
    filesystem refused anything."""

    def _lines(title: str, items: "tuple[Artifact, ...]") -> None:
        typer.echo(f"{title} ({len(items)}):")
        for item in items:
            typer.echo(f"  {item.render()}")

    reports = [report for _, report in done]
    verb = "would remove" if dry_run else "removed"
    # `removed` cannot collide across roots -- every prunable artifact is
    # under the root that planned it -- so the merge is a no-op there and
    # the byte total below is over the same set. The lists that DO collide
    # are the floored ones: two roots of one lineage floor one anchor
    removed = _merged(*(report.removed for report in reports))
    held = _merged(*(report.held for report in reports))
    floored = _merged(*(report.floored for report in reports))
    failed = tuple(pair for report in reports for pair in report.failed)
    refused = tuple(pair for report in reports for pair in report.refused)
    _lines(verb, removed)
    _lines("prunable, outside the flags given", _merged(*(report.kept for report in reports)))
    _lines("held (floor lifted, no class licenses it)", held)
    _lines("floored (the model refuses)", floored)
    if refused:
        # NOT the filesystem: nothing was attempted and nothing is broken.
        # The archive asked for a period whose eligibility changed between
        # the plan and the receipt, and the operator has an order to follow
        typer.echo(f"the archive refused ({len(refused)}):", err=True)
        for item, reason in refused:
            typer.echo(f"  {item.path}: {reason}", err=True)
    if failed:
        typer.echo(f"the filesystem refused ({len(failed)}):", err=True)
        for item, reason in failed:
            typer.echo(f"  {item.path}: {reason}", err=True)
    if estate_wide:
        typer.echo(f"roots planned ({len(done)}):")
        for plan, _ in done:
            typer.echo(
                f"  {plan.run_root}: period {plan.current_period},"
                f" attested {sorted(plan.attested) or 'none'}"
            )
    if stopped is not None:
        refuse(stopped)
    first = done[0][0]
    tail = (
        f" -- estate {first.estate_id}, {len(done)} root(s) planned"
        if estate_wide
        else f" -- estate {first.estate_id}, period {first.current_period},"
        f" attested {sorted(first.attested) or 'none'}"
    )
    typer.echo(
        f"{verb} {len(removed)} artifact(s),"
        f" {sum(report.bytes_removed for report in reports)} byte(s);"
        f" {len(floored)} floored, {len(held)} held{tail}"
    )
    # an archive refusal exits 2 for the same reason a filesystem one does:
    # the operator asked for something that did not happen, and a zero exit
    # would say it did. A DRY RUN did not ask -- it surveyed -- so its
    # refusals print and its exit stays 0
    return bool(failed) or (bool(refused) and not dry_run)


def _merged(*verdicts: "tuple[Artifact, ...]") -> "tuple[Artifact, ...]":
    """One verdict list across every root that was planned, in walk order
    and without a repeat.

    The anchor is a SIBLING of every root it fences, so two roots of one
    lineage each floor the same `anchor.json` and the same `anchor.lock`.
    Concatenating would report one estate's two floored anchors, and a
    count an operator cannot reconcile with the disk is a report that
    lies."""
    seen: set[tuple[str, str]] = set()
    out: list[Artifact] = []
    for verdict in verdicts:
        for item in verdict:
            key = (str(item.path), item.kind)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return tuple(out)
