"""The estate verbs: the period boundary and what lives around it (DL-137).

`seal` closes the running period and commits the next, live or offline (period-model ss7);
`audit` re-derives a closed period and writes its attestation, `verify` validates one;
`estate reclaim` moves a stale successor claim out of the way and `estate prune` deletes what
retention allows; `estate args` prints a verb's arguments from the estate manifest (DL-407).
`cli.py` assembles the `estate` sub-app, with `estate build` and `estate diff` from
`cli_estate_build.py` (DL-413).
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import typer

from dsl41.cli_common import (
    PERMIT_UNKNOWN,
    PROPERTIES,
    SupervisorChoice,
    check_base_tz,
    command_outcome,
    confirmed_resume_refusal,
    IN_PROGRESS,
    guarded_maintenance,
    lineage_of,
    ping_probe,
    load_catalog_and_ast_or_exit_2,
    load_semantics,
    load_tz_aliases,
    read_header_of,
    refuse,
    resume_target_period,
    say_next,
    settle_maintenance,
    statechart_report,
    supervisor_exited,
    walk_estate_or_exit_2,
)
from dsl41.estate_manifest import ArgsFor, ManifestError, build_args
from dsl41.period import ARCHIVE_CLASS
from dsl41.runner_clock import RealClock
from dsl41.runner_ledger import LOCK_NAME, LeaderLock

if TYPE_CHECKING:
    from dsl41.boundary import EstateAnchor, EstateWalk, SealRequest
    from dsl41.attest import Attestation
    from dsl41.retention import Artifact, PruneReport, RetentionPlan
    from dsl41.ir import CatalogIR
    from dsl41.period import Manifest, RuntimeProfile, StagedManifest
    from dsl41.runner import Engine
    from dsl41.runner_adapters import SupervisorConn
    from dsl41.runner_startup import SupervisorLaunch, Wiring
    from dsl41.seal import Seal, StagedNextPeriod


# ------------------------------------------------------- the boundary (U7)

_RUN_ROOT_OPT = typer.Option(..., "--run-root", help="The estate root.")  # period-model ss1.1

#: the same option where the verb ALSO has an estate-wide mode: naming the anchor alone points at
#: the lineage rather than at one root (PR-02f)
_ESTATE_ROOT_OPT = typer.Option(
    None,
    "--run-root",
    help="The estate root. Omit it and give --estate-anchor alone to work on"
    " the whole estate: every root in the registry, in period order.",
)  # period-model ss1.1

_ANCHOR_OPT = typer.Option(
    None,
    "--estate-anchor",
    help="The lineage anchor directory. Defaults to <run-root>.anchor, a"
    " sibling of the run root. A rolled root's anchor is the lineage's and"
    " must be named.",
)  # period-model ss1.3

_ACTOR_OPT = typer.Option(
    None,
    "--claimed-actor",
    help="Who is asking, for the log. This is a claim, not an authorization:"
    " this tier has no authentication. Defaults to <user>@<host>.",
)  # control-protocol ss7 gap 2


# The options `seal` and `estate quiesce` share: both seal the running period into the next one.
NEXT_FILES_OPT = typer.Option(
    ...,
    "--next",
    help="JIL files of the next period's estate. Repeatable; the order is part of the bundle hash.",
)
FORCE_SEAL_OPT = typer.Option(
    False,
    "--force-seal",
    help="Commit inside the closing period's retry horizon. Recorded"
    " in the seal as force_seal: true, with the gate's numbers when"
    " the gate was engaged.",
)  # period-model ss9
REQUEST_ID_OPT = typer.Option(
    None,
    "--request-id",
    help="Reuse the id of an earlier attempt to retry it exactly. A"
    " committed boundary answers its exact retry from the next"
    " period. Any other id is a fresh request.",
)
NEXT_TIMEZONE_OPT = typer.Option(
    None,
    "--next-timezone",
    help="The next period's timezone for schedules that do not set their own.",
)
NEXT_TIMEZONE_MAP_OPT = typer.Option(
    None,
    "--next-timezone-map",
    help="The next period's timezone map. See 'dsl41 run --help' for --timezone-map.",
)  # SEM-35/DL-62
NEXT_AS_MACHINE_OPT = typer.Option(
    [],
    "--next-as-machine",
    help="Machine names the next period runs as. Repeatable. See"
    " 'dsl41 run --help' for --as-machine.",
)  # DL-52
NEXT_MACHINE_POLICY_OPT = typer.Option(
    "strict",
    "--next-machine-policy",
    help="The next period's machine policy: strict or local-eligible.",
)
NEXT_DETACHED_OPT = typer.Option(
    False,
    "--next-detached",
    help="The next period runs command jobs under the supervisor.",
)  # ss6a Tier 1
NEXT_DEADMAN_OPT = typer.Option(
    None,
    "--next-deadman",
    help="The next period's supervisor deadman, in seconds. Needs --next-detached.",
)
NEXT_SEMANTICS_OPT = typer.Option(
    [],
    "--next-semantics",
    metavar="NAME=VALUE",
    help="The next period's semantic switches. Repeatable. See 'dsl41 run --help' for --semantics.",
)  # runner-design ss8a, DL-252


def _next_profile(
    timezone: "str | None",
    timezone_map: "Path | None",
    as_machine: list[str],
    machine_policy: str,
    detached: bool,
    deadman: "float | None",
    semantics: "list[str] | None" = None,
) -> "RuntimeProfile":
    """C2's `RuntimeProfile` from the `--next-*` flags (period-model ss2.1).

    Prefixed because a boundary names two periods and these flags describe the one about to open.
    One gate for every `--next-*` flag (DL-145): it catches `RuntimeProfile`'s own refusal
    (machine-policy literal, positive deadman) rather than copying it, so `--next-deadman 0` is a
    clean exit 2. The model cannot hold the PAIRING below, which spans two flags."""
    from pydantic import ValidationError

    from dsl41.period import runtime_profile_from_cli

    if deadman is not None and not detached:
        # loud, not silent: without a supervisor nothing holds the lifelines a deadman could
        # bound (concurrency-model ss8); `run` says the same of its own flags
        raise typer.Exit(
            refuse("--next-deadman needs --next-detached: a tethered run has no supervisor")
        )
    tz_aliases = load_tz_aliases(timezone_map)
    check_base_tz(timezone, tz_aliases)
    overrides = load_semantics(semantics, option="--next-semantics")
    try:
        return runtime_profile_from_cli(
            timezone=timezone,
            tz_aliases=tz_aliases,
            as_machine=as_machine,
            machine_policy=machine_policy,
            detached=detached,
            deadman_s=deadman,
            semantics=overrides,
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
    """ss7's staging, both modes: the immutable bundle, then `staged_manifest.json` and
    `candidate.json` under `periods/.staging/<stage_digest>/`.

    Content-addressed, so a repeat is idempotent and a concurrent client writing the same bytes is
    harmless, which makes it safe against a LIVE engine's root without its lock."""
    from dsl41.boundary import stage_next_period, stage_period

    catalog, parsed, _ = load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)
    staged_manifest = stage_period(run_root, parsed, catalog, profile)
    staged = stage_next_period(run_root, staged_manifest=staged_manifest)
    return staged, staged_manifest, catalog


def seal(
    next_files: list[Path] = NEXT_FILES_OPT,
    run_root: Path = _RUN_ROOT_OPT,
    estate_anchor: Path = _ANCHOR_OPT,
    force_seal: bool = FORCE_SEAL_OPT,
    claimed_actor: str = _ACTOR_OPT,
    request_id: str = REQUEST_ID_OPT,
    next_timezone: str = NEXT_TIMEZONE_OPT,
    next_timezone_map: Path = NEXT_TIMEZONE_MAP_OPT,
    next_as_machine: list[str] = NEXT_AS_MACHINE_OPT,
    next_machine_policy: str = NEXT_MACHINE_POLICY_OPT,
    next_detached: bool = NEXT_DETACHED_OPT,
    next_deadman: float = NEXT_DEADMAN_OPT,
    next_semantics: list[str] = NEXT_SEMANTICS_OPT,
    supervisor: SupervisorChoice = typer.Option(
        SupervisorChoice.attach,
        "--supervisor",
        metavar="MODE",
        help="Offline, for a detached period: how this command reaches the"
        " period's supervisor, attach or spawn. attach, the default, connects to the running"
        " one and refuses when none answers. spawn starts one with the"
        " period's pinned deadman, for development. A live seal ignores it.",
    ),  # DL-396
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Close the running period and commit the next estate.

    --next names the JIL files of the next period. Who leads the run
    root decides how the seal happens. Live: an engine leads it, so
    this command stages the next catalog and asks the engine over the
    control socket; the engine runs the cutoff and exits with code 3.
    Offline: nothing leads it, so this command takes the locks, runs
    recovery, and performs the boundary itself. The lock decides which,
    not a flag. Offline, a detached period needs its supervisor running:
    with none answering, the seal refuses unless --supervisor spawn.

    After the seal, open the next period with 'dsl41 run --resume' on
    the same root or 'dsl41 run --open-from' into a fresh one. A period
    change is a restart, not a reload.

    Exit codes: 0 the boundary committed; 2 it did not commit and the
    period is still open, or another boundary was in flight and this
    request did nothing; 4 the outcome is unknown, or this request's own
    seal is still in flight, and the printed request_id is the only safe
    way to retry.
    """
    # Design: period-model ss7
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
        next_semantics,
    )
    actor = claimed_actor or default_actor()
    try:
        lock = acquire_run_root(run_root)
    except EngineError:
        # the lock IS the discriminator: probing a socket answers a different question (a socket
        # file outlives the process that made it)
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
                    supervisor.value,
                )
            )
        )
    finally:
        lock.release()


def _committed_boundary(run_root: Path, request_id: "str | None") -> "Seal | None":
    """The committed boundary `--request-id` names, if this root's LINEAGE holds one
    (period-model ss2.2, DL-169).

    ss2.2 promises that an exact retry of a committed boundary is answered from the next period.
    The engine keys that answer on the request fingerprint, which covers the `baseline_id` and
    `epoch` of the original attempt (DL-151), so the seal recovers the boundary-time envelope here.

    The proof is `cli_common.lineage_of` (`boundary.select_seal`), never which sidecar files exist:
    `period.sealed_periods` can list an ORPHAN sidecar written between a crash and the `seal`
    record's WAL append, and ss2.2 says "an orphan sidecar is ignored by rule" and "only a
    committed seal is ever deduplicated" (DL-169). `select_seal` is the lineage proof the live
    engine carries in `EstateHome.prior_seal_record` (`ControlServer._committed_seal`), through
    the `seal` record branch or the `opens_from_seal` digest-link branch (PR-02a/PR-30e).

    It is read fresh rather than taken from `_offline_seal`'s resumed engine, which would skip
    `select_seal`'s digest re-verification and turn a missing sidecar's ss11 refusal into a
    silent `None`. `_live_seal` reads a WAL a live engine may be appending to; the WAL is
    append-only, so a racing read can only tear the last line, which is dropped
    (`runner_journal.read_journal`)."""
    if request_id is None:
        return None
    from dsl41.cli_common import lineage_of

    lineage = lineage_of(run_root)
    if lineage is None:
        return None
    seal = lineage.seal
    return seal if seal is not None and seal.boundary_request.request_id == request_id else None


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
    """ss7 live mode: stage C2, then ask the leading engine for the boundary over the control
    socket.

    The CLI stages first and names the staged bytes by `stage_digest`; the engine validates exactly
    those bytes, so two clients racing on one root stage under two fingerprints and the engine
    commits the one its request names. The answer is read by `cli_common.command_outcome`
    (DL-92, DL-137); ss7's table is 0/2/4, and an answer it cannot classify stays `unknown`."""
    import uuid

    from dsl41.runner_control import ControlClientError, roundtrip

    socket_path = run_root / "control.sock"
    if not socket_path.exists():
        return refuse(
            f"{run_root}: an engine holds leader.lock and {socket_path} is not there --"
            " a live seal is asked for over the control socket, and this root has a"
            " leader with no door (period-model ss7)"
        )
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
        # ss2.2's retry route: the envelope the ORIGINAL attempt carried, the only one the
        # engine's stored decision answers to
        baseline, epoch = retry.baseline_id, retry.epoch
    # `staged` is the OWNER's projection (stage_next_period's return, DL-137): which fields cross
    # from launcher-pin to client-proposal
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
        # the anchor the caller named rides into the printed resume command (DL-145): a ROLLED
        # root's anchor is the LINEAGE's and must be spelled explicitly
        on_applied=lambda: say_next(run_root, estate_anchor),
        # ss7 publishes 0/2/4 for this verb and 3 means something else here
        # (a sealed ENGINE exits 3); see `command_outcome`
        rejected_as_unknown=True,
        # one boundary at a time: a refusal that names THIS request's id is
        # its own boundary still running (unknown, 4), another id is a no-op (2)
        seal_in_flight_by_id=True,
    )


def _closing_manifest(run_root: Path) -> "Manifest":
    """The manifest `_offline_seal` closes against; raises `EngineError` when the root cannot
    give one."""
    from dsl41.period import read_period_manifest
    from dsl41.runner_clock import EngineError

    try:
        # the period this sealer will CLOSE; on a root with a committed boundary that is the one
        # `resume_run` opens (DL-151)
        pinned = read_period_manifest(run_root, resume_target_period(run_root))
    except Exception as exc:  # RunHistoryError or EngineError: both are refusals
        raise EngineError(f"{run_root}: {exc}") from exc
    if pinned is None:
        raise EngineError(
            f"{run_root}: no period manifest -- an offline seal reads the CLOSING"
            " period's identity from the root itself, and this root has none"
            " (period-model ss7)"
        )
    return pinned


def _load_closing_catalog(run_root: Path, pinned: "Manifest") -> "tuple[CatalogIR, datetime]":
    """The CLOSING period's catalog and the clock reading its preflight used; raises
    `EngineError` as the loader does, with the preflight's refusal text on failure."""
    from datetime import UTC, datetime

    from dsl41.runner_clock import EngineError

    from dsl41.boundary import load_bundle_catalog, preflight_errors

    # the OWNER's loader (DL-145) wraps JilParseError/LoweringError as EngineError, which
    # `_offline_seal`'s `except EngineError` handles.
    #
    # `permit_unknown`: these are the exact bytes the CLOSING period ran, the unknown-attribute
    # gate ran once at launch, and re-asking it would make `dsl41 seal` refuse a root `dsl41 run`
    # is serving.
    catalog = load_bundle_catalog(run_root, pinned.source_bundle_hash, permit_unknown=True)
    # DL-240: the live path's boundary check (`preflight_errors`) sees only the SUCCESSOR catalog
    # (ss10.1); offline sealing dispatches the CLOSING period's own catalog first, so it needs
    # this gate before any adapter is wired.
    now = datetime.now(UTC).replace(tzinfo=None)
    errors = preflight_errors(catalog, pinned.runtime_profile, at=now)
    if errors:
        raise EngineError(
            f"{run_root}: the closing period's catalog fails preflight under"
            f" this build: {'; '.join(errors)}; drain and recreate"
            " the estate (DL-240)"
        )
    return catalog, now


def _offline_seal_request(
    engine: "Engine | None",
    retry: "Seal | None",
    request_id: "str | None",
    staged: "StagedNextPeriod",
    *,
    force_seal: bool,
    actor: str,
) -> "SealRequest":
    """The `SealRequest` the offline sealer submits as its one boundary. A retry of a committed
    boundary needs no engine; a fresh request takes its envelope from the resumed one."""
    import uuid

    from dsl41.boundary import SealRequest

    # ss2.2's retry route: a retry carries the envelope its ORIGINAL attempt carried, because
    # the stored decision is fingerprinted over it. The engine's CURRENT header, which
    # `resume_run` has moved to the next period, names a different command, and offline
    # nothing would recognise the retry before it closed a SECOND period (DL-151).
    envelope: "Seal | Engine | None" = retry if retry is not None else engine
    if envelope is None:
        raise ValueError("a fresh seal request takes its envelope from the resumed engine")
    return SealRequest(
        baseline_id=envelope.baseline_id,
        epoch=envelope.epoch,
        request_id=request_id or str(uuid.uuid4()),
        next_period=staged,
        stage_digest=staged.stage_digest,
        force_seal=force_seal,
        claimed_actor=actor,
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
    supervisor: "SupervisorLaunch",
) -> int:
    """ss7 offline mode: no engine, so this process leads exactly one boundary.

    The caller holds `leader.lock`, which tells the two modes apart. `resume_run` is the same-root
    recovery barrier (`anchor.lock`, a `leader` record at epoch+1, replay, reconcile, recorded
    kills), and the boundary is the same `submit_seal` a live engine serves. C1 is loaded from the
    ROOT's own bundle, never the command line: the root outlives the estate files, and the closing
    period's identity is the manifest's."""
    from dsl41.runner_admission import refuse_reserved_request_id
    from dsl41.runner_clock import EngineError, RealClock
    from dsl41.period import wal_path
    from dsl41.runner_startup import SupervisorAbsent, resume_run, wire_from_profile

    # this path builds its `SealRequest` without an envelope, so the reserved prefix is refused
    # here, as `parse_envelope` does on the socket
    if request_id is not None:
        try:
            refuse_reserved_request_id(request_id)
        except EngineError as exc:
            return refuse(exc)
    # ss1.3's resume rule (DL-224), before anything stages C2 into the root or wires a supervisor;
    # `leader.lock` is already held
    refusal = confirmed_resume_refusal(run_root, estate_anchor)
    if refusal is not None:
        return refuse(refusal)
    try:
        pinned = _closing_manifest(run_root)
    except EngineError as exc:
        return refuse(exc)
    staged, staged_manifest, _ = _stage_next(
        run_root, next_files, profile, permit_unknown, properties
    )
    # an exact retry of a committed boundary is answered from the lineage on disk before any
    # supervisor is wired: the answer needs no engine (period-model ss2.2, DL-396)
    early = _committed_boundary(run_root, request_id)
    if early is not None:
        answered = _answer_from_committed(
            early,
            _offline_seal_request(
                None, early, request_id, staged, force_seal=force_seal, actor=actor
            ),
            run_root,
            estate_anchor,
            # no recovery ran here: the next period is open on this root only if its WAL
            # segment exists (period-model ss1.1), as on a root rolled from the anchor (DL-169)
            next_period=(
                "open" if wal_path(run_root, early.next_period.period_id).exists() else "ready"
            ),
        )
        if answered is not None:
            return answered
    wiring: "Wiring | None" = None
    #: what the body raised, so the lease's teardown does not mask it
    failed: BaseException | None = None
    try:
        try:
            catalog, now = _load_closing_catalog(run_root, pinned)
            wiring = await wire_from_profile(
                run_root,
                catalog,
                pinned.runtime_profile,
                start=now,
                supervisor=supervisor,
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
                machines=wiring.machines,
            )
        except SupervisorAbsent as exc:
            return refuse(
                f"{exc}; start the supervisor service, or pass --supervisor spawn to start one"
                " in this session"
            )
        except EngineError as exc:
            return refuse(exc)
        retry = _committed_boundary(run_root, request_id)
        request = _offline_seal_request(
            engine, retry, request_id, staged, force_seal=force_seal, actor=actor
        )
        answered = _answer_from_committed(
            retry, request, run_root, estate_anchor, next_period="opened here"
        )
        if answered is not None:
            await _close_engine(engine)
            return answered
        return await _drive_boundary(engine, request, run_root, estate_anchor)
    except BaseException as error:
        failed = error
        raise
    finally:
        # the lease goes back on every way out after the wiring (DL-145), a statechart error from
        # the resume included
        if wiring is not None:
            await _give_back(wiring, failed)


async def _give_back(wiring: "Wiring", failed: BaseException | None) -> None:
    """Return the supervisor lease and close the client; the first error is the one reported, and
    the teardown's own is printed once."""
    from dsl41.state_machine import StatechartError

    if failed is None:
        await wiring.close()
        return
    try:
        await wiring.close()
    except Exception as second:  # noqa: BLE001 -- printed; the first error goes on
        if isinstance(second, StatechartError):
            statechart_report(second)
        else:
            typer.echo(f"dsl41: giving back the supervisor lease failed: {second!r}", err=True)


def _answer_from_committed(
    seal: "Seal | None",
    request: "SealRequest",
    run_root: Path,
    estate_anchor: "Path | None",
    *,
    next_period: "Literal['opened here', 'open', 'ready']",
) -> int | None:
    """ss2.2's retry route offline: answer from the boundary that already committed, or None.

    `next_period` is where the root stands, said truthfully for each path (DL-396): `opened here`
    when this command's recovery opened it; `open` when the early answer from the lineage on disk
    finds its WAL segment (period-model ss1.1), as on a root rolled from the anchor; `ready` when
    it finds none.

    The live path gets this from the period N+1 engine's `seal` record
    (`ControlServer._committed_seal`). Offline, `runner_control.committed_retry` is applied to the
    sidecar: an EXACT retry is the original answer, and the same id under a different envelope is
    a collision (PR-30c, PR-30e)."""
    from dsl41.runner_control import RETRY_SWAP_REASON, committed_retry

    if seal is None:
        return None
    retry = committed_retry(
        request.request_id,
        request.fingerprint,
        committed_request_id=seal.boundary_request.request_id,
        committed_fingerprint=seal.request_fingerprint,
    )
    if retry is None:
        return None
    if retry == "collision":
        return refuse(
            f"request_id {request.request_id} already named the boundary that closed"
            f" period {seal.period_id} under a different envelope: " + RETRY_SWAP_REASON
        )
    period = seal.next_period.period_id
    where = {
        "opened here": f"This root is at period {period}, which the recovery this command ran"
        " has opened",
        "open": f"This root is at period {period}, already open",
        "ready": f"period {period} is ready to open",
    }[next_period]
    typer.echo(
        f"period {seal.period_id} was already closed by request_id"
        f" {request.request_id}: seal {seal.digest}. No second boundary. {where}"
    )
    say_next(run_root, estate_anchor)
    return 0


async def _close_engine(engine: "Engine") -> None:
    """Give back what an offline sealer took, whatever became of the boundary: the engine's loop
    and its journal.

    The teardown is a detach-stop (runner-design ss6a case b): a run the closing period carries
    live keeps running under the supervisor, and the next period reattaches to it (PR-30b)."""
    import contextlib

    engine.detach.stopping = True
    with contextlib.suppress(Exception):
        await engine.shutdown()
    if engine.journal is not None:
        engine.journal.close()


async def _drive_boundary(
    engine: "Engine", request: "SealRequest", run_root: Path, estate_anchor: "Path | None"
) -> int:
    """One queued boundary, driven to its outcome by this process's own loop.

    The LOOP ending is a committed boundary (`PeriodSealed`), a fail-stop, or a crash. The
    REQUEST's future ending while the loop runs on is a refusal: `abort_boundary` reopened
    admission and the period is still open, which is ss7's exit 2. Exits are ss7's: 0 committed,
    2 refused with C1 still open, 4 unknown (retry only with the printed `request_id`), 7 a
    statechart error (DL-327)."""
    import asyncio
    import contextlib

    from datetime import datetime

    from dsl41.boundary import BoundaryFailStop, PeriodSealed
    from dsl41.state_machine import StatechartError

    # `ensure_future` hands back the engine's own future and gives the three-way classification
    # its `done`/`exception`/`result` surface
    future = asyncio.ensure_future(engine.submit_seal(request))
    loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    await asyncio.wait({future, loop_task}, return_when=asyncio.FIRST_COMPLETED)
    code = 2
    outcome: BaseException | None = None
    if future.done() and future.exception() is None:
        # The FUTURE decides, not the loop: once it holds a committed boundary the boundary
        # committed, whatever the loop does afterwards. Reading the loop first would report exit 2
        # (ss7: "it did NOT commit and the period is still open") for an unrelated teardown
        # failure. The loop is still awaited, because it is a bounded number of turns from its
        # own `PeriodSealed`, the single authority for the operator's sentence; anything else it
        # raises is printed as diagnostics.
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
    elif isinstance(outcome, StatechartError):
        # an assertion failure, not a refusal: the boundary neither aborted
        # nor moved, and the sealer stops with its report and exit 7
        # (statechart-engine ss5, DL-327)
        code = statechart_report(outcome)
    elif outcome is not None:
        # ss7 publishes 0/2/4/7 for this verb and nothing else (DL-145): whatever exception said
        # so, the boundary did NOT commit and C1 is still open
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
        None, "--period", help="Audit only this period. Without it, every closed period."
    ),
) -> None:
    """Re-derive a closed period and write its attestation.

    Verified means re-derived. This command rebuilds the seal from the
    period's own evidence (the opening seal, the ordered journal, the
    spool, and both manifests) and refuses when the rebuilt seal and
    the stored one disagree, naming the fields.

    Attesting period N needs the previous period's attestation present
    and verified. Period 1 is the base case. There is no option to
    skip this, because a checkpoint over an unaudited opening seal
    would let earlier roots be deleted on a chain that was never
    established.

    A period archived by 'estate prune --archive-inputs' has no inputs
    left to re-derive from. For it, this command verifies the
    checkpoint and reports attestation-verified, a weaker result than a
    re-derivation.

    With --estate-anchor and no --run-root, every period in the
    registry is audited in period order, each in the root that holds
    it. A root the registry names but the disk lacks is refused.

    'dsl41 verify' checks one attestation instead. That is what a
    rolled root can do; a full audit is not.

    Exit codes: 0 every requested period is attested; 2 any refusal.
    """
    # Design: period-model ss1.3, ss11, DL-144, PR-02f
    from dsl41.boundary import EstateAnchor, default_anchor_dir

    if run_root is None and estate_anchor is None:
        raise typer.Exit(
            refuse(
                "name a --run-root, or --estate-anchor alone to audit every root the"
                " registry names (period-model ss1.3, PR-02f)"
            )
        )
    # the anchor is taken by `audit_period` for the ONE write that needs it and released, so
    # auditing a closed period while a later one is live is possible: a leader holds the lineage
    # lock for its life
    skipped: list[str] = []
    if run_root is None:
        walk = walk_estate_or_exit_2(estate_anchor)
        anchor = EstateAnchor(estate_anchor)
        targets, skipped = _closed_across_roots(walk, period)
        where = f"{walk.anchor_dir} (estate {walk.estate_id})"
    else:
        anchor = EstateAnchor(estate_anchor or default_anchor_dir(run_root))
        # ARCHIVED periods are named too: `closed_periods` asks for a sidecar AND the segment that
        # re-derives it, which an archived period lacks; dropping it would answer with a smaller
        # estate than the one on disk (DL-144)
        chosen = [period] if period is not None else _root_periods(run_root)
        targets = [(period_id, run_root) for period_id in chosen]
        where = str(run_root)
    if not targets:
        for line in skipped:
            typer.echo(line)
        raise typer.Exit(refuse(f"{where}: no closed period to audit"))
    from dsl41.runner_clock import EngineError

    try:
        _attest_each(targets, anchor, name_root=run_root is None)
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc
    finally:
        # in a `finally` so a refusal partway through the walk still reports what was not audited
        for line in skipped:
            typer.echo(line)


def _root_periods(run_root: Path) -> list[int]:
    """The periods a SINGLE-ROOT audit is about (DL-144).

    Three sets: `closed_periods` wants a sidecar and its re-deriving segment; an ARCHIVED period
    has the sidecar and no segment on purpose; a period this root WROTE whose segment is gone with
    no receipt is loss, to be met and refused rather than dropped."""
    from dsl41.period import archived_periods, closed_periods, sealed_periods, wrote_period

    # the bound comes from the SIDECARS this root holds, never from `closed_periods`, which is
    # empty exactly when every closed period lost its segment (DL-144)
    lost = {
        period_id for period_id in sealed_periods(run_root) if wrote_period(run_root, period_id)
    }
    return sorted(set(closed_periods(run_root)) | set(archived_periods(run_root)) | lost)


def _closed_across_roots(
    walk: "EstateWalk", period: "int | None"
) -> tuple[list[tuple[int, Path]], list[str]]:
    """The registry's periods that are CLOSED, each with its own root, and a line for each period
    that is not.

    An OPEN period is not audit's to re-derive. The lines come back rather than printing here so
    they land after the attestations."""
    from dsl41.period import closed_periods

    if period is not None and not any(entry.period_id == period for entry in walk.periods):
        why = (
            "has a registry row whose first segment is not durable yet, and every"
            " cross-period reader ignores a row until it is"
            if period in walk.provisional
            else "is in no registry row of this lineage"
        )
        raise typer.Exit(refuse(f"{walk.anchor_dir}: period {period} {why} (period-model ss1.3)"))
    targets: list[tuple[int, Path]] = []
    skipped: list[str] = []
    for entry in walk.periods:
        if period is not None and entry.period_id != period:
            continue
        # an ARCHIVED period is closed and not re-derivable, so it is a target, not a skip:
        # `_attest_each` verifies its checkpoint and reports the attestation-verified tier
        # (DL-144)
        if entry.archived is not None or entry.period_id in closed_periods(entry.root):
            targets.append((entry.period_id, entry.root))
        else:
            skipped.append(
                f"period {entry.period_id} in {entry.root}: not closed, nothing to audit"
            )
    return targets, skipped


def _attest_each(
    targets: list[tuple[int, Path]], anchor: "EstateAnchor", *, name_root: bool
) -> int:
    """Re-derive each (period, root) in turn and report it; both audit modes and `estate
    quiesce` share this loop. Returns how many checkpoints wait for their registry row; a
    refusal raises `EngineError`."""
    from dsl41.attest import (
        ATTESTATION_VERIFIED,
        DERIVATION_VERIFIED,
        Unattested,
        audit_period,
        verified_tier,
    )

    outstanding = 0
    for period_id, root in targets:
        at = f" in {root}" if name_root else ""
        tier = verified_tier(root, period_id)
        try:
            # SEAL-ONLY or not, this is the same call: `audit_period` re-derives when the
            # inputs are there and consumes the stored checkpoint when not (PR-02e), and only
            # one of the two paths may report itself as a derivation. Skipping an archived
            # period would also skip the `attested` row a busy lineage lock left unflipped.
            attestation = audit_period(root, period_id, anchor=anchor)
        except Unattested as exc:
            # the checkpoint IS written and durable; only the registry row, which is
            # bookkeeping, is not. `Unattested` means the lineage lock was busy, the ordinary
            # state of an estate with a live engine, so it is this period's note and never the
            # walk's reason to stop
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
    if outstanding:
        typer.echo(
            f"{outstanding} checkpoint(s) are durable with the registry row"
            " outstanding: re-run this when the lineage lock is free -- the audit is"
            " idempotent and finishes the row (period-model ss1.3)",
            err=True,
        )
    return outstanding


def _archived_line(root: Path, period_id: int, at: str, attestation: "Attestation") -> str:
    """One archived period, verified and reported at ss11's OTHER tier (DL-144).

    The wording shares no phrase with the derivation-verified line, so an operator scanning rows
    sees which periods can still be re-derived."""
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
        None, "--period", help="Verify this period's attestation. Without it, the newest."
    ),
) -> None:
    """Check an attestation's digest, seal binding and chain position.

    It checks the attestation alone, without re-walking the chain. The
    audit that produced it established the chain, and a physical roll
    imports only the current seal and attestation. So a root that
    imported seal 2 and attestation 2, with its predecessors gone,
    still verifies the chain below seal 2.

    Exit codes: 0 it verifies; 2 otherwise.
    """
    # Design: period-model ss1.3
    from dsl41.attest import verify_attestation
    from dsl41.period import attestation_path, attestation_periods
    from dsl41.runner_clock import EngineError

    if period is None:
        held = attestation_periods(run_root)
        if not held:
            raise typer.Exit(refuse(f"{run_root}: no attestation to verify"))
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
        ..., "--estate-anchor", help="The lineage anchor directory."
    ),  # period-model ss1.3
    force: bool = typer.Option(
        False, "--force", help="Required. This is the one operation that can fork a lineage."
    ),
    claimed_actor: str = _ACTOR_OPT,
) -> None:
    """Move a stale successor claim out of the way (break-glass).

    A claimed lineage head whose target root is unreachable looks the
    same as one whose target is merely paused. Nothing here can tell
    them apart; you must. If the claimant is still alive, this forks
    the lineage: two roots open the same period, allocate the same
    indices, and run the same job runs twice. Prove the claimant is
    gone before you run this.

    The reclaim is recorded in the anchor and in the next segment
    record, with the actor who claimed to authorize it.

    Exit codes: 0 the head moved; 2 otherwise.
    """
    # Design: period-model ss1.3
    from dsl41.boundary import EstateAnchor
    from dsl41.runner_clock import EngineError
    from dsl41.runner_control import claimed_actor as default_actor

    if not force:
        raise typer.Exit(
            refuse(
                "refusing without --force: reclaiming a live claimant's head forks the"
                " lineage, and this verb exists for the case where you have PROVED it is"
                " gone (period-model ss1.3)"
            )
        )
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
        help="Remove run tombstones (the run directory, its .by_run_id"
        " entry and default logs) of runs that have ended in attested"
        " periods.",
    ),
    quarantine: bool = typer.Option(
        False,
        "--quarantine",
        help="Remove quarantined candidates: staged periods that were"
        " superseded and that no recovery references.",
    ),
    archive_inputs: bool = typer.Option(
        False,
        f"--{ARCHIVE_CLASS}",
        help="Archive a covered period: write its receipt, then delete"
        " its journal and committed candidate files. Irreversible. The"
        " period can then only be attestation-verified. Prune its"
        " tombstones first.",
    ),
    older_than_days: float = typer.Option(
        None,
        "--older-than-days",
        help="Keep any run spool touched more recently than this many days.",
    ),
    keep_runs: int = typer.Option(
        0,
        "--keep-runs",
        help="Keep the newest N run spools of each job, whatever else"
        " says. Counted per job and, estate-wide, per root.",
    ),
) -> None:
    """Delete what retention allows and report what it keeps.

    Retention policy is yours; the flags state it. The floors are the
    model's: everything reachable from the lineage head stays, whatever
    the flags say. That covers the sentinel, the anchor and any live
    claim, the sidecars this period opened from and will close with,
    the current and next manifests, an uncommitted candidate, their
    bundles, the latest attestation, and the journal and spool of any
    unattested period.

    The report has six rows:
      removed (would remove, under --dry-run): what went.
      prunable, outside the flags given: could go, but no flag asked for it.
      held: released by the head moving on, but kept because no retention
        flag covers it. The row says which dependency is in the way.
      floored: below the model's floor. This command cannot remove it.
      the archive refused: selected but not allowed yet. Nothing was
        attempted. The reason names the order to follow.
      the filesystem refused: selected and allowed, but the operating
        system refused. A partial sweep, reported as one.

    --archive-inputs is irreversible. A period is archived only when it
    is attested, a later checkpoint covers it, its spool is already
    pruned, and every older period in this root is archived. The
    receipt (seals/<period>.archive.json) is written before the first
    deletion. After it, the period can only be attestation-verified,
    never re-derived. Restoring the files does not undo it.

    Pruning a tombstone is also irreversible: the period cannot
    be re-derived from its own evidence, and its attestation stands for
    it. Attest first, then prune.

    With --estate-anchor and no --run-root, every root in the registry
    is planned and reported as one result, in period order. Floors and
    refusals are per root, and --keep-runs counts per job and per root,
    which keeps more than asked and never less.

    Exit codes: 0 every selected artifact is removed, or listed under
    --dry-run; 2 a refusal, or the filesystem refused a removal. The
    report says which ones went.
    """
    # Design: period-model ss11a, ss12, PR-36b, PR-36c, DL-144 (PR-Q3), ss1.3,
    # PR-02f
    from dsl41.retention import CLASSES, plan_retention, prune
    from dsl41.runner_clock import EngineError

    if run_root is None and estate_anchor is None:
        raise typer.Exit(
            refuse(
                "name a --run-root, or --estate-anchor alone to prune every root the"
                " registry names (period-model ss1.3, PR-02f)"
            )
        )
    # the PAIRING derives from retention.CLASSES, the canon of which classes exist (DL-152's
    # pattern, as cli_control._QUERY_VERBS off runner_access.REQUIRED_TIER): a class added without
    # a flag here is a KeyError, not a silent gap (DL-178h).
    flag_of = {"tombstones": tombstones, "quarantine": quarantine, ARCHIVE_CLASS: archive_inputs}
    classes = [name for name in CLASSES if flag_of[name]]
    if not classes and dry_run:
        # a listing with no class named is a survey of every licensed deletion
        classes = sorted(CLASSES)
    if not classes and not dry_run:
        raise typer.Exit(
            refuse(
                "nothing selected: name at least one class (--tombstones, --quarantine,"
                f" --{ARCHIVE_CLASS}) or ask for --dry-run. A prune verb with a default"
                " set would be a retention policy, and that is the operator's"
                " (period-model ss12)"
            )
        )
    if run_root is None:
        roots = walk_estate_or_exit_2(estate_anchor).roots()
    else:
        roots = (run_root,)
    done: list[tuple[RetentionPlan, PruneReport]] = []
    stopped: str | None = None
    for root in roots:
        # the try is INSIDE the loop, so a refusal on the third root still reports what the
        # first two deleted; the report of which artifacts went is this verb's promise
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
        # nothing was planned, so there is no report to keep: the refusal alone
        if stopped is None:
            # `roots` is never empty (the walk refuses a registry with no durable period), so an
            # empty `done` is a break, and a break sets `stopped`
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
    """One report over every root that was planned; answers whether the filesystem refused
    anything."""

    def _lines(title: str, items: "tuple[Artifact, ...]") -> None:
        typer.echo(f"{title} ({len(items)}):")
        for item in items:
            typer.echo(f"  {item.render()}")

    reports = [report for _, report in done]
    verb = "would remove" if dry_run else "removed"
    # `removed` cannot collide across roots (each artifact is under the root that planned it), so
    # the merge is a no-op there. The floored lists do collide: two roots of one lineage floor one
    # anchor
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
        # NOT the filesystem: nothing was attempted. The archive asked for a period whose
        # eligibility changed between the plan and the receipt; the operator has an order to follow
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
    # an archive refusal exits 2 like a filesystem one: the operator asked for something that
    # did not happen. A DRY RUN only surveyed, so its refusals print and its exit stays 0
    return bool(failed) or (bool(refused) and not dry_run)


def _merged(*verdicts: "tuple[Artifact, ...]") -> "tuple[Artifact, ...]":
    """One verdict list across every root planned, in walk order without repeats.

    The anchor is a SIBLING of every root it fences, so two roots of one lineage each floor the
    same `anchor.json` and `anchor.lock`; concatenating would double-count them."""
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


# ------------------------------------------------------- the estate stop (DL-398)
#
# `estate quiesce` is the shape 1 estate stop as one verb. It runs as the service account and
# needs no root. The two root steps stay with the operator and are checked, not performed: the
# reboot hold and the stopped engine unit. Then it leads the run root, seals and audits the
# running period while the supervisor runs, asks the supervisor for guarded maintenance
# (DL-390), and checks that neither unit came back and nothing writes. Each stage ends with one
# line. The verb never waits for a precondition: a stage that cannot pass yet refuses with what
# to wait for, and a stage already done is skipped, so a rerun is safe. Its one bounded wait is
# the watch of an admitted maintenance operation, its `bound_s` and a margin at most; a rerun
# watches the operation `maintenance.json` names and never requests it twice (DL-401).

#: a reader of the service manager: `systemctl` argv in, its stdout out
SystemctlReader = Callable[[list[str]], str]

#: the example units' names (examples/nightbank/deploy)
ENGINE_UNIT = "dsl41-engine.service"
SUPERVISOR_UNIT = "dsl41-supervisor.service"

#: ActiveState values of a unit that is down; `failed` is a sealed engine's exit 3
_UNIT_DOWN = ("inactive", "failed")


def read_systemctl(argv: list[str]) -> str:
    """The real reader. `systemctl show` needs no privilege."""
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=5.0).stdout


def _gone(error: OSError) -> bool:
    """Whether a failed read of `/proc/<pid>` proves that the process has gone."""
    return isinstance(error, FileNotFoundError | ProcessLookupError) or error.errno in (
        errno.ENOENT,
        errno.ESRCH,
    )


def _proc_status(pid: str) -> dict[str, str]:
    status = Path(f"/proc/{pid}/status").read_text()
    return {
        key: value.strip()
        for key, _, value in (line.partition(":") for line in status.splitlines())
    }


def _foreign(entry: Path, uid: str) -> bool:
    """Whether another uid owns `/proc/<pid>`, or the process has gone; a stat that fails
    otherwise proves neither."""
    try:
        return str(_proc_owner(entry)) != uid
    except OSError as exc:
        if _gone(exc):
            return True
        raise OSError(f"cannot observe pid {entry.name} ({exc})") from exc


def _proc_owner(entry: Path) -> int:
    return entry.stat().st_uid


def _ancestors() -> set[str]:
    """This process and its parent chain: the shell or sudo that started the verb writes
    nothing in the run root."""
    chain: set[str] = set()
    pid = str(os.getpid())
    while pid not in chain and pid not in ("0", ""):
        chain.add(pid)
        try:
            pid = _proc_status(pid).get("PPid", "")
        except OSError:
            break
    return chain


def other_processes() -> list[str] | None:
    """Every other live process of this effective uid, as `pid (command)`; None off Linux.

    The verb and its ancestors are left out, and so is a zombie: it has exited and holds no file.
    Only a read that proves the process gone skips it, or a denied read of a process another uid
    owns (`/proc` mounted `hidepid`). Any other failed read raises `OSError`, because a process
    that cannot be observed is not shown absent."""
    if not sys.platform.startswith("linux"):
        return None
    mine, uid = _ancestors(), str(os.geteuid())
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal() or entry.name in mine:
            continue
        try:
            fields = _proc_status(entry.name)
        except OSError as exc:
            if _gone(exc) or (exc.errno in (errno.EACCES, errno.EPERM) and _foreign(entry, uid)):
                continue
            raise OSError(f"cannot observe pid {entry.name} ({exc})") from exc
        uids = fields.get("Uid", "").split()
        if len(uids) < 2 or uids[1] != uid or fields.get("State", "").startswith("Z"):
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            raw = b""
        command = raw.replace(b"\0", b" ").decode(errors="replace").strip()
        found.append(f"{entry.name} ({command or fields.get('Name', '?')})")
    return found


def _supervisor_conn(sock_path: Path) -> SupervisorConn:
    from dsl41.runner_adapters import SupervisorConn

    return SupervisorConn(sock_path)


@dataclass(frozen=True)
class QuiesceHost:
    """What the verb reads of the machine around it. Tests replace each part."""

    systemctl: SystemctlReader = read_systemctl
    #: naive UTC, the journal's clock domain (runner_clock.RealClock)
    now: Callable[[], datetime] = RealClock().now
    sleep: Callable[[float], None] = time.sleep
    connect: Callable[[Path], SupervisorConn] = _supervisor_conn
    processes: Callable[[], list[str] | None] = other_processes


#: the host `estate quiesce` reads
QUIESCE_HOST = QuiesceHost()


class _HeldLeaderLock(LeaderLock):
    """`leader.lock` as the verb holds it (S6a). The offline seal's engine gives its proofs back
    when its journal closes; leadership stays with the verb until `let_go`."""

    def release(self) -> None:
        """The seal's engine, or its refused resume, returns the lock; the verb keeps it."""

    def let_go(self) -> None:
        super().release()

    def holder(self) -> str:
        """Who the lock file records as its holder, for a refusal."""
        return self._holder()


@dataclass(frozen=True)
class _QuiesceRequest:
    run_root: Path
    estate_anchor: Path | None
    next_files: list[Path]
    profile: RuntimeProfile
    permit_unknown: bool
    properties: list[Path] | None
    force_seal: bool
    actor: str
    request_id: str | None
    force: str | None
    engine_unit: str
    supervisor_unit: str


#: what an unknown maintenance outcome asks of the operator (DL-390, DL-401)
_UNKNOWN_MAINTENANCE = (
    "the outcome is unknown: read maintenance.json, the unit, the process and supervisor.log"
    " before anything else; never repeat it blindly (deployment-runbook ss3)"
)


class _Stop(Exception):
    """A stage that did not pass, with its exit code."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class _Quiesce:
    """One run of the stages, in order. Each stage method prints its line or raises `_Stop`."""

    def __init__(self, request: _QuiesceRequest, host: QuiesceHost) -> None:
        self.request = request
        self.host = host
        self.lock = _HeldLeaderLock(request.run_root)
        #: what this run or an earlier one made durable, for a refusal line
        self.done: list[str] = []
        self.sealed: int | None = None
        self.supervisor_pid: int | None = None
        #: the supervisor stage's LIST answer, when one answered
        self.listing: dict[str, Any] = {}
        self.exited_pid: int | None = None

    # ------------------------------------------------------------------ output

    def say(self, stage: str, outcome: str) -> None:
        typer.echo(f"quiesce: {stage}: {outcome}")

    def refuse(self, stage: str, reason: str, code: int = 2) -> _Stop:
        durable = f"; durable: {', '.join(self.done)}" if self.done else ""
        self.say(stage, f"refused: {reason}{durable}")
        return _Stop(code)

    # ------------------------------------------------------------------ stages

    def run(self) -> int:
        from dsl41.state_machine import StatechartError

        try:
            for stage in (
                self.hold,
                self.engine,
                self.leadership,
                self.supervisor,
                self.horizon,
                self.seal,
                self.audit,
                self.maintenance,
                self.units,
                self.writers,
            ):
                try:
                    stage()
                except typer.Exit as exit_:
                    # a door the stage shares with another verb printed its own refusal
                    raise self.refuse(stage.__name__, f"exited {exit_.exit_code}", exit_.exit_code)
                except StatechartError:
                    self.say(stage.__name__, "refused: a statechart error, reported on stderr")
                    raise
        except _Stop as stop:
            return stop.code
        finally:
            self.lock.let_go()
        return 0

    def _unit(self, stage: str, unit: str) -> dict[str, str]:
        argv = ["systemctl", "show", unit]
        for name in ("LoadState", "UnitFileState", "ActiveState", "SubState", "MainPID"):
            argv += ["-p", name]
        try:
            output = self.host.systemctl(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            raise self.refuse(stage, f"cannot read {unit}: {exc}") from exc
        props = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if props.get("LoadState") not in ("loaded", "masked"):
            raise self.refuse(
                stage,
                f"{unit} is not a loaded unit ({props.get('LoadState', '?')}); name the units"
                " with --engine-unit and --supervisor-unit",
            )
        return props

    def hold(self) -> None:
        """The reboot hold: both units disabled or masked, so a reboot inside the window starts
        nothing."""
        units = (self.request.engine_unit, self.request.supervisor_unit)
        enabled = [
            f"{unit} is {state or 'unknown'}"
            for unit in units
            if (state := self._unit("hold", unit).get("UnitFileState"))
            not in ("disabled", "masked")
        ]
        if enabled:
            raise self.refuse(
                "hold", f"{'; '.join(enabled)}; as root: systemctl disable {' '.join(units)}"
            )
        self.say("hold", "ok")

    def engine(self) -> None:
        unit = self.request.engine_unit
        props = self._unit("engine", unit)
        if props.get("ActiveState") not in _UNIT_DOWN:
            raise self.refuse(
                "engine",
                f"{unit} is {props.get('ActiveState')}/{props.get('SubState')};"
                f" as root: systemctl stop {unit}",
            )
        self.say("engine", "ok")

    def leadership(self) -> None:
        from dsl41.runner_clock import EngineError

        root = self.request.run_root
        path = root / LOCK_NAME
        if path.is_symlink() or not path.is_file():
            raise self.refuse(
                "leadership",
                f"no {LOCK_NAME} in {root}: a used run root has one, and maintenance"
                " refuses without it (deployment-runbook ss3, cold bootstrap)",
            )
        try:
            self.lock.acquire()
        except EngineError as exc:
            holder = self._operation_holding_lock()
            if holder is None:
                raise self.refuse(
                    "leadership", f"{exc}; find the process that holds {path}"
                ) from exc
            self.sealed = self._sealed_period()
            if self.sealed is None:
                # the seal needs the lock; the operation keeps it until its exit (DL-401)
                raise self.refuse(
                    "leadership",
                    f"the supervisor (pid {holder}) holds {path} for an admitted maintenance"
                    " operation and the period is not sealed; rerun once it has ended",
                ) from exc
            # the period is sealed, so no later stage needs the lock: a rerun watches it
            self.say("leadership", "skipped: the supervisor holds it for maintenance (DL-401)")
        except OSError as exc:
            raise self.refuse("leadership", f"{path}: {exc}") from exc
        else:
            self.say("leadership", "ok")
            self.sealed = self._sealed_period()

    def _operation_holding_lock(self) -> int | None:
        """The pid of the live supervisor whose admitted or uncertain operation
        `maintenance.json` records, or None: from admission to its exit that supervisor holds
        `leader.lock` (DL-390, DL-401). An unreadable record names nobody."""
        from dsl41.runner_supervisor import read_maintenance_record

        try:
            record = read_maintenance_record(self.request.run_root)
            if record is None or record["state"] not in ("admitted", "uncertain"):
                return None
            pid: int = record["supervisor_pid"]
            return None if supervisor_exited(pid, record["supervisor_start_time"]) else pid
        except (OSError, ValueError):
            return None

    def _sealed_period(self) -> int | None:
        """The period this root holds sealed and not yet opened, or None."""
        lineage = lineage_of(self.request.run_root)
        if lineage is None or not lineage.opens_next or lineage.seal is None:
            return None
        self.done.append(f"period {lineage.seal.period_id} sealed")
        return lineage.seal.period_id

    def supervisor(self) -> None:
        from dsl41.runner_clock import EngineError

        root = self.request.run_root
        sock = root / "supervisor.sock"
        try:
            conn = self.host.connect(sock)
            try:
                listing = conn.send({"cmd": "LIST"})
            finally:
                conn.close()
        except (OSError, ValueError, EngineError) as exc:
            listing = {"ok": False, "error": str(exc)}
        if listing.get("ok") is True:
            self.supervisor_pid = listing.get("supervisor_pid")
            self.listing = listing
            live = [run for run in listing.get("runs", []) if run.get("wrapper_alive")]
            self.say("supervisor", f"ok: LIST answers, {len(live)} live run(s)")
            return
        if (root / "supervisor.pid").exists() or sock.exists():
            raise self.refuse(
                "supervisor",
                f"no LIST answers on {sock} ({listing.get('error')}) and its files remain:"
                f" wait for {self.request.supervisor_unit} to restart, or follow"
                " deployment-runbook ss3 for an unresponsive supervisor",
            )
        if self.sealed is not None:
            self.say("supervisor", "skipped: already gone")
        else:
            # a tethered period needs none; a detached one makes the seal refuse with its remedy
            self.say("supervisor", "skipped: none answers")

    def horizon(self) -> None:
        """period-model ss9's retry horizon, read before the seal so the refusal names its end."""
        from dsl41.boundary import externally_requested_attempts
        from dsl41.period import estate_wal
        from dsl41.runner_clock import EngineError
        from dsl41.runner_journal import read_journal

        if self.sealed is not None:
            self.say("horizon", "skipped: already sealed")
            return
        root = self.request.run_root
        try:
            horizon_us = _closing_manifest(root).runtime_profile.retry_horizon_us
            attempts = externally_requested_attempts(read_journal(estate_wal(root)))
        except (OSError, ValueError, EngineError) as exc:
            raise self.refuse("horizon", str(exc)) from exc
        if not attempts:
            self.say("horizon", "ok: no operator request in this period")
            return
        # the journal's `at` and `Host.now` are both naive UTC (runner_clock)
        last = max(datetime.fromisoformat(str(attempt["at"])) for attempt in attempts)
        end = last + timedelta(microseconds=horizon_us)
        stamp = f"{end.isoformat(timespec='seconds')} UTC"
        if self.host.now() >= end:
            self.say("horizon", f"ok: ended at {stamp}")
        elif self.request.force_seal:
            self.say("horizon", f"ok: runs until {stamp}; --force-seal records the force")
        else:
            raise self.refuse(
                "horizon",
                f"the retry horizon runs until {stamp}; rerun after it, or pass --force-seal",
            )

    def seal(self) -> None:
        if self.sealed is not None:
            self.say(
                "seal",
                f"skipped: already sealed: period {self.sealed};"
                f" period {self.sealed + 1} waits to be opened",
            )
            return
        r = self.request
        code = asyncio.run(
            _offline_seal(
                r.run_root,
                r.estate_anchor,
                r.next_files,
                r.profile,
                r.permit_unknown,
                r.properties,
                r.force_seal,
                r.actor,
                r.request_id,
                self.lock,
                "attach",
            )
        )
        if code == 4:
            raise self.refuse(
                "seal", "its outcome is unknown; rerun with the --request-id it printed", 4
            )
        if code != 0:
            raise self.refuse("seal", f"the offline seal exited {code}, the period is open", code)
        self.sealed = self._sealed_period()
        if self.sealed is None:
            raise self.refuse("seal", "the seal reported success and the root holds no seal", 4)
        self.say("seal", f"ok: period {self.sealed} sealed")

    def audit(self) -> None:
        from dsl41.attest import read_attestation
        from dsl41.boundary import EstateAnchor, default_anchor_dir
        from dsl41.runner_clock import EngineError

        root = self.request.run_root
        targets = _root_periods(root)
        if not targets:
            raise self.refuse("audit", f"{root}: no closed period to audit")
        already = all(read_attestation(root, period) is not None for period in targets)
        anchor = EstateAnchor(self.request.estate_anchor or default_anchor_dir(root))
        try:
            outstanding = _attest_each([(p, root) for p in targets], anchor, name_root=False)
        except EngineError as exc:
            raise self.refuse("audit", str(exc)) from exc
        if outstanding:
            raise self.refuse(
                "audit",
                f"{outstanding} registry row(s) wait for the lineage lock; rerun when it is free",
            )
        self.done.append(f"attested through period {targets[-1]}")
        if already:
            self.say("audit", f"skipped: already attested through period {targets[-1]}")
        else:
            self.say("audit", f"ok: attested through period {targets[-1]}")

    def maintenance(self) -> None:
        """Guarded maintenance, or, on a rerun, the admitted operation `maintenance.json` names:
        it is watched to its outcome and never requested again (DL-401)."""
        from dsl41.runner_supervisor import read_maintenance_record

        root = self.request.run_root
        try:
            record = read_maintenance_record(root)
        except ValueError as exc:
            raise self.refuse("maintenance", f"{exc}; see deployment-runbook ss3", 4) from exc
        if self.supervisor_pid is None and record is None:
            self.say("maintenance", "skipped: no supervisor")
            return
        if self.supervisor_pid is None or self.listing.get("process_state") != "serving":
            self._settle(record)
            return
        sock = root / "supervisor.sock"
        try:
            conn = self.host.connect(sock)
        except OSError as exc:
            raise self.refuse("maintenance", f"{sock}: {exc}") from exc
        try:
            code, reply = guarded_maintenance(
                conn,
                sock,
                controller_id=f"estate-quiesce-{os.getpid()}",
                force=self.request.force,
                # the supervisor takes leader.lock itself for maintenance and holds it through
                # its teardown (DL-390); the verb hands it over with the request
                before_stop=self.lock.let_go,
                on_refusal=lambda incarnation, token: self._report_and_release(conn, token),
                sleep=self.host.sleep,
                connect=self.host.connect,
            )
        finally:
            conn.close()
        if code == 0 and reply is not None:
            self.exited_pid = reply.get("supervisor_pid")
            self.say("maintenance", f"ok: completed; the supervisor (pid {self.exited_pid}) exited")
            return
        if code == 3:
            raise self.refuse("maintenance", IN_PROGRESS, 3)
        if code == 4:
            raise self.refuse("maintenance", _UNKNOWN_MAINTENANCE, 4)
        raise self.refuse("maintenance", self._maintenance_remedy(reply or {}))

    def _settle(self, record: dict[str, Any] | None) -> None:
        """A rerun meets an admitted operation. The owner rule runs first: the record must name
        the supervisor LIST shows, or with none answering, the one the start note names; any
        other record is not a maintenance outcome, 4 (DL-401). Then an exited supervisor whose
        record says completed is skipped, a live one is watched for the rest of its bound, and
        uncertain is 4."""
        from dsl41.runner_supervisor import record_owner_problem

        if record is None:
            raise self.refuse(
                "maintenance",
                f"the supervisor is {self.listing.get('process_state')} and no maintenance.json"
                f" names it; {_UNKNOWN_MAINTENANCE}",
                4,
            )
        pid = record["supervisor_pid"]
        incarnation = self.listing.get("incarnation")
        if incarnation is None:
            problem = record_owner_problem(self.request.run_root, record)
        elif record["incarnation"] != incarnation:
            problem = "maintenance.json names another supervisor than the one LIST shows"
        else:
            problem = None
        if problem is not None:
            raise self.refuse("maintenance", f"{problem}; {_UNKNOWN_MAINTENANCE}", 4)
        try:
            if record["state"] == "completed" and supervisor_exited(
                pid, record["supervisor_start_time"]
            ):
                self.exited_pid = pid
                self.say(
                    "maintenance",
                    f"skipped: already completed; the supervisor (pid {pid}) exited",
                )
                return
            code, reason, _ = settle_maintenance(
                self.request.run_root,
                incarnation,
                sleep=self.host.sleep,
                probe=ping_probe(self.request.run_root / "supervisor.sock", self.host.connect),
            )
        except (OSError, ValueError) as exc:
            code, reason = 4, str(exc)
        if code == 3:
            raise self.refuse("maintenance", reason, 3)
        if code != 0:
            raise self.refuse("maintenance", f"{reason}; {_UNKNOWN_MAINTENANCE}", 4)
        self.exited_pid = pid
        self.say("maintenance", f"ok: {reason}")

    def _maintenance_remedy(self, reply: dict[str, Any]) -> str:
        """A refusal's cause and its own next move."""
        error, detail = reply.get("error"), str(reply.get("detail") or "")
        if error == "lease_held":
            return (
                f"the supervisor's lease is held by {reply.get('holder')} until"
                f" {reply.get('expires_at')}; find that controller, then rerun"
            )
        if error == "not_admitted":
            return f"the reply was lost and the request was {detail}; rerun"
        if error != "maintenance_refused":
            return f"the supervisor answered {error}; see deployment-runbook ss3"
        if "active wrappers" in detail:
            return f"{detail}; the jobs go on: rerun once they end, or pass --force terminate"
        if LOCK_NAME in detail or self._lock_unavailable():
            return (
                f"the supervisor could not take {self.lock.path} ({detail}), held by"
                f" {self.lock.holder()}; find the process that holds it, then rerun"
            )
        return f"{detail}; containment and unit policy: see deployment-runbook ss3"

    def _lock_unavailable(self) -> bool:
        """Whether `leader.lock` cannot be taken now: the supervisor's refusal does not always
        name the lock it could not take."""
        from dsl41.runner_procid import flock_existing

        try:
            fd = flock_existing(self.lock.path)
        except OSError:
            return True
        os.close(fd)
        return False

    def _report_and_release(self, conn: SupervisorConn, token: int) -> None:
        """A refused maintenance: print the live runs, then give the lease back."""
        try:
            listing = conn.send({"cmd": "LIST"})
            for run in listing.get("runs", []):
                if run.get("wrapper_alive"):
                    typer.echo(
                        f"quiesce: live run: {run.get('job')} run {run.get('run_number')}"
                        f" ({run.get('run_id')}), wrapper pid {run.get('wrapper_pid')}"
                    )
            conn.send({"cmd": "RELEASE", "token": token})
        except Exception as exc:  # noqa: BLE001 -- a lost lease is freed by the connection's close
            typer.echo(f"quiesce: listing or releasing after the refusal failed: {exc}", err=True)

    def units(self) -> None:
        """Both units down, read once after the observed exit. A restart policy that did not
        keep the stop shows as `activating`/`auto-restart`, or as a new main process."""
        stopping: list[str] = []
        back: list[str] = []
        for unit in (self.request.engine_unit, self.request.supervisor_unit):
            props = self._unit("units", unit)
            active, sub = props.get("ActiveState"), props.get("SubState")
            state = f"{unit} is {active}/{sub}"
            if active in _UNIT_DOWN and sub in ("dead", "failed"):
                continue
            if active == "deactivating" or (
                unit == self.request.supervisor_unit
                and active == "active"
                and self.exited_pid is not None
                and props.get("MainPID") == str(self.exited_pid)
            ):
                stopping.append(state)  # the manager has not finished recording the exit
            else:
                back.append(state)
        if back:
            raise self.refuse(
                "units",
                f"{'; '.join(back)}: its exit policy did not keep the stop; read the unit"
                " before anything else",
                4,
            )
        if stopping:
            raise self.refuse("units", f"{'; '.join(stopping)}; rerun once it is inactive")
        self.say("units", "ok")

    def writers(self) -> None:
        """deployment-runbook ss2b's no-writers check, then leadership goes."""
        from dsl41.runner_clock import EngineError

        root = self.request.run_root
        left = [name for name in ("supervisor.pid", "supervisor.sock") if (root / name).exists()]
        if left:
            raise self.refuse("writers", f"{', '.join(left)} still in {root}")
        if not self.lock.held:
            try:
                self.lock.acquire()
            except (EngineError, OSError) as exc:
                raise self.refuse(
                    "writers", f"{exc}; find the process that holds {self.lock.path}"
                ) from exc
        try:
            others = self.host.processes()
        except OSError as exc:
            raise self.refuse("writers", f"{exc}; rerun") from exc
        if others is None:
            self.say("writers", "skipped: not linux (no supervisor files; leader.lock free)")
            return
        if others:
            raise self.refuse(
                "writers", f"processes of uid {os.geteuid()} remain: {', '.join(others)}"
            )
        self.say("writers", "ok")


def estate_quiesce(
    run_root: Path = _RUN_ROOT_OPT,
    estate_anchor: Path = _ANCHOR_OPT,
    next_files: list[Path] = typer.Option(
        ...,
        "--next",
        help=f"{NEXT_FILES_OPT.help} A rerun after the seal reads no file and ignores them.",
    ),
    force_seal: bool = FORCE_SEAL_OPT,
    claimed_actor: str = _ACTOR_OPT,
    request_id: str = REQUEST_ID_OPT,
    next_timezone: str = NEXT_TIMEZONE_OPT,
    next_timezone_map: Path = NEXT_TIMEZONE_MAP_OPT,
    next_as_machine: list[str] = NEXT_AS_MACHINE_OPT,
    next_machine_policy: str = NEXT_MACHINE_POLICY_OPT,
    next_detached: bool = NEXT_DETACHED_OPT,
    next_deadman: float = NEXT_DEADMAN_OPT,
    next_semantics: list[str] = NEXT_SEMANTICS_OPT,
    force: str = typer.Option(
        None,
        "--force",
        metavar="terminate",
        help="Maintenance only: terminate the supervisor's known command groups"
        " deliberately, as 'supervise shutdown --force=terminate'.",
    ),
    engine_unit: str = typer.Option(ENGINE_UNIT, "--engine-unit", help="The engine's unit."),
    supervisor_unit: str = typer.Option(
        SUPERVISOR_UNIT, "--supervisor-unit", help="The supervisor's unit."
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Stop a shape 1 estate: seal, audit and guarded maintenance in one verb.

    Run it as the service account after two root steps, which it
    checks: the reboot hold (both units disabled) and the stopped
    engine unit. It takes leader.lock, seals the running period offline
    and audits it while the supervisor runs, then asks the supervisor
    for guarded maintenance. It reads both units once and checks that
    nothing writes in the run root.

    Each stage prints one line. A stage that cannot pass yet refuses
    and names what to wait for; run the verb again. A stage already
    done is skipped, so a quiesced estate runs to exit 0 again.

    Exit codes: 0 the estate is stopped; 2 a stage refused, and what
    the earlier stages did is durable; 3 maintenance is admitted and
    still runs: it finishes on its own, so run the verb again; 4
    uncertain: a maintenance outcome maintenance.json does not prove, a
    unit that came back, or a seal whose outcome is unknown.
    """
    # Design: DL-398, DL-401, deployment-runbook ss2b
    from dsl41.runner_control import claimed_actor as default_actor

    if force is not None and force != "terminate":
        raise typer.Exit(refuse("--force takes one value: terminate"))
    profile = _next_profile(
        next_timezone,
        next_timezone_map,
        next_as_machine,
        next_machine_policy,
        next_detached,
        next_deadman,
        next_semantics,
    )
    request = _QuiesceRequest(
        run_root=run_root,
        estate_anchor=estate_anchor,
        next_files=next_files,
        profile=profile,
        permit_unknown=permit_unknown,
        properties=properties,
        force_seal=force_seal,
        actor=claimed_actor or default_actor(),
        request_id=request_id,
        force=force,
        engine_unit=engine_unit,
        supervisor_unit=supervisor_unit,
    )
    raise typer.Exit(_Quiesce(request, QUIESCE_HOST).run())


class ArgsFormat(str, Enum):
    """`estate args --format`: one shell-quoted line, or a JSON document (DL-407)."""

    line = "line"
    json = "json"


def estate_args(
    manifest: Path = typer.Argument(
        ..., help="The estate manifest, estate.toml, in the release directory."
    ),
    verb: ArgsFor = typer.Option(
        ...,
        "--for",
        metavar="VERB",
        help="The verb to build arguments for: run, seal, lint, rehearse or viz.",
    ),
    environment: str = typer.Option(
        None,
        "--environment",
        help="The environment whose properties layers follow the manifest's own. Required when"
        " the manifest declares environments.",
    ),
    output: ArgsFormat = typer.Option(
        ArgsFormat.line,
        "--format",
        metavar="FORMAT",
        help='line: one shell-quoted line. json: {"argv": [...]}.',
    ),
) -> None:
    """Print the arguments one verb takes for the estate a manifest declares.

    The manifest is the application's one declaration of its catalog
    files, properties layers, timezone and semantic switches: the
    interpretation half of the runtime profile. The deployment half
    stays with the launcher: --detached, --deadman, --as-machine and
    --machine-policy, which every seal restates as --next-*, because an
    omitted --next-* option takes its default. Add those and --run-root
    after the printed arguments.

    Give MANIFEST as an absolute path. Paths are the manifest's names
    joined to its directory, unresolved, because the bundle hash covers
    the path strings.

    Exit codes: 0 the arguments are printed; 2 the manifest is relative,
    cannot be read or validated, or the environment is missing or not
    declared.
    """  # DL-407, period-model ss1.1
    try:
        argv = build_args(manifest, verb, environment)
    except ManifestError as exc:
        raise typer.Exit(refuse(exc)) from exc
    typer.echo(shlex.join(argv) if output is ArgsFormat.line else json.dumps({"argv": argv}))
