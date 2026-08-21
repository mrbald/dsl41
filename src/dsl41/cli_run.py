"""The runner verbs: execute an estate, rehearse one, read one back
(DL-137's split).

`run` and `rehearse` drive the engine -- one on the wall clock with real
processes, one on the virtual clock with scripted adapters, both through
the same `start_run`/`resume_run` path. `journal` and `runs` are their
offline readers: a WAL replayed through a fresh oracle, and run history
folded out of one or more run roots. Registered on the app in `cli.py`.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, cast

import typer

from dsl41.ast_jil import JilFile, render_statement
from dsl41.boundary import PeriodSealed
from dsl41.cli_common import (
    PERMIT_UNKNOWN,
    PROPERTIES,
    TIMEZONE_MAP_OPT,
    TIMEZONE_OPT,
    check_base_tz,
    import_tui_or_exit_2,
    load_catalog_and_ast_or_exit_2,
    load_catalog_or_exit_2,
    load_tz_aliases,
    refuse,
    say_next,
)
from dsl41.ir import CatalogIR
from dsl41.period import root_is_unused

if TYPE_CHECKING:
    from datetime import datetime

    from dsl41.period import RuntimeProfile, StagedManifest
    from dsl41.runner_history import RunRow


# ------------------------------------------------------------------- runner (phase 11)
#
# Exit codes for the runner verbs: 0 clean (run: operator-stopped; rehearse:
# quiescent), 1 the engine/estate failed while running (EngineError, oracle
# refusal mid-run), 2 the run never started (preflight ERROR, resume gate,
# unreadable scenario, unreachable socket). The control-plane verbs read the
# same 0/1/2 and split 2 three ways on top; that half of the note went with
# them, to cli_control.py.


def _preflight_or_exit(
    catalog: CatalogIR,
    *,
    execution: bool,
    machine_policy: str = "strict",
    as_machine: "list[str] | None" = None,
    start: "datetime | None" = None,
    tz_aliases: "dict[str, str] | None" = None,
) -> list:
    """Print ss8 findings; exit 2 on any ERROR; return the WARNs (the caller
    journals them next to the run -- WARN prints, journals, and runs).
    `start` anchors the DL-56 calendar-exhaustion WARN: run passes wall-now,
    rehearse its virtual --start."""
    from dsl41.runner_preflight import MachinePolicy, preflight

    if machine_policy not in ("strict", "local-eligible"):
        typer.echo(f"--machine-policy {machine_policy!r}: expected strict|local-eligible", err=True)
        raise typer.Exit(2)
    items = preflight(
        catalog,
        execution=execution,
        machine_policy=cast("MachinePolicy", machine_policy),
        as_machine=frozenset(as_machine or ()),
        start=start,
        tz_aliases=tz_aliases,
    )
    for item in items:
        target = f" {item.job}" if item.job else ""
        typer.echo(
            f"preflight {item.severity} [{item.code}]{target}: {item.message}",
            err=item.severity == "ERROR",
        )
    if any(item.severity == "ERROR" for item in items):
        typer.echo("preflight: refusing to run (runner-design ss8)", err=True)
        raise typer.Exit(2)
    return items


def _naive_utc_arg(text: str, option: str) -> "datetime":
    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise typer.Exit(refuse(exc, prefix=option)) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _spec_texts(parsed: "list[JilFile]", catalog: CatalogIR) -> "dict[str, str]":
    """job -> preserve-rendered block for the ss10 `spec` verb: every
    statement whose subject is a catalog job, concatenated in file order
    (insert_job plus any later update_job/delete_job the estate carries)."""
    texts: dict[str, str] = {}
    for jf in parsed:
        for stmt in jf.statements:
            if stmt.subject in catalog.jobs:
                block = render_statement(stmt)
                texts[stmt.subject] = texts.get(stmt.subject, "") + block
    return texts


def run(
    files: list[Path] = typer.Argument(..., help="JIL files forming the estate to execute"),
    run_root: Path = typer.Option(
        ..., "--run-root", help="Run directory (journal, runs/, logs/, control.sock)."
    ),
    resume: bool = typer.Option(
        False, "--resume", help="Resume the run_root's journal (replay + reconcile, ss7)."
    ),
    open_from: Path = typer.Option(
        None,
        "--open-from",
        help="PHYSICAL ROLL: open the next period into this FRESH --run-root from the"
        " lineage this ANCHOR DIRECTORY names (period-model ss7). The head must be"
        " `closed`, the closing period must be quiescent and ATTESTED (`dsl41"
        " audit`), and the target must satisfy ss1.1's ownership rule. The anchor is"
        " the lineage's, so pass the same --estate-anchor on every later --resume.",
    ),
    estate_anchor: Path = typer.Option(
        None,
        "--estate-anchor",
        help="The lineage anchor directory (period-model ss1.3). Defaults to"
        " <run-root>.anchor -- a sibling of the root, never inside it, because the"
        " root is what an operator archives.",
    ),
    ui: bool = typer.Option(
        False, "--ui", help="Attach the ss11 Textual TUI in this terminal (quit stops the run)."
    ),
    detached: bool = typer.Option(
        False,
        "--detached",
        help="Run CMD jobs under a per-run-root supervisor (ss6a Tier 1) so an"
        " engine restart reattaches instead of killing them; stopping the engine"
        " leaves jobs running -- resume with --resume --detached.",
    ),
    deadman: float = typer.Option(
        None,
        "--deadman",
        help="SECONDS with no live controller after which the supervisor exits,"
        " killing every job it holds by lifeline EOF (concurrency-model ss8)."
        " Needs --detached. This is what makes `dsl41 host evict` provable: a"
        " run root without it is never reroutable except by force. It costs the"
        " thing --detached buys, so choose it longer than any planned engine"
        " outage -- an engine down longer than this loses its jobs.",
    ),
    machine_policy: str = typer.Option(
        "strict",
        "--machine-policy",
        help="How to treat a job on a virtual pool split across this host and"
        " others: 'strict' (default) refuses it; 'local-eligible' runs it here"
        " with a WARN (pool placement ignored). Machines are resolved through"
        " insert_machine (node_name / members); a job pinned to another host is"
        " always refused (DL-49).",
    ),
    as_machine: list[str] = typer.Option(
        [],
        "--as-machine",
        help="Machine name(s) this runner IS (DL-52), e.g. --as-machine"
        " greezy_spoon. A job whose machine: is (or resolves through"
        " insert_machine to) one of these runs here; anything else is refused"
        " foreign. Repeatable. Omit for zero-config (the forward hostname; no"
        " reverse-DNS). Declaring is explicit and drops all hostname guessing.",
    ),
    timezone: str = TIMEZONE_OPT,
    timezone_map: Path = TIMEZONE_MAP_OPT,
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Execute the estate headlessly on this machine: wall clock, real
    processes, WAL journal, calendar scheduler, and the control socket
    (runner-design ss1/ss9/ss10). Runs until stopped (SIGINT/SIGTERM);
    tethered (default) engine death terminates all jobs, durably recorded
    (ss6a); `--detached` keeps jobs alive under a supervisor across engine
    restarts. Drive it with `dsl41 sendevent` / `dsl41 query`, or attach the
    TUI (`--ui` here, or `dsl41 ui` from another terminal).
    """
    import asyncio

    from datetime import UTC, datetime

    if ui:
        import_tui_or_exit_2()  # fail before the engine starts, not after
    if open_from is not None:
        if resume:
            typer.echo(
                "--open-from and --resume are the two OPENERS and you get one:"
                " --resume continues the lineage in this root, --open-from opens the"
                " next period into a fresh one (period-model ss7)",
                err=True,
            )
            raise typer.Exit(2)
        if estate_anchor is not None and Path(estate_anchor) != Path(open_from):
            typer.echo(
                f"--open-from {open_from} IS the lineage anchor; --estate-anchor"
                f" {estate_anchor} names another one (period-model ss7)",
                err=True,
            )
            raise typer.Exit(2)
        estate_anchor = open_from
    catalog, parsed, fingerprint = load_catalog_and_ast_or_exit_2(
        files, permit_unknown, properties
    )
    tz_aliases = load_tz_aliases(timezone_map)
    warns = _preflight_or_exit(
        catalog,
        execution=True,
        machine_policy=machine_policy,
        as_machine=as_machine,
        start=datetime.now(UTC).replace(tzinfo=None),
        tz_aliases=tz_aliases,
    )
    check_base_tz(timezone, tz_aliases)
    if deadman is not None and not detached:
        # loud, not silent: without a supervisor there is nothing to hold the
        # lifelines, so nothing a deadman could bound (concurrency-model ss8)
        typer.echo("--deadman needs --detached: a tethered run has no supervisor", err=True)
        raise typer.Exit(2)
    if deadman is not None and deadman <= 0:
        typer.echo("--deadman must be a positive number of seconds", err=True)
        raise typer.Exit(2)
    from dsl41.runner_clock import EngineError
    from dsl41.period import runtime_profile_from_cli

    # ss2.1: the launch options that change interpretation or dispatch, as
    # one typed value -- `runtime_hash` is what tells a period launched
    # --timezone UTC from the same JIL launched --timezone Europe/Zurich
    profile = runtime_profile_from_cli(
        timezone=timezone,
        tz_aliases=tz_aliases,
        as_machine=as_machine,
        machine_policy=machine_policy,
        detached=detached,
        deadman_s=deadman,
    )
    try:
        raise typer.Exit(
            asyncio.run(
                _serve_run(
                    catalog,
                    run_root,
                    resume,
                    warns,
                    profile=profile,
                    ui=ui,
                    spec_texts=_spec_texts(parsed, catalog),
                    estate_fingerprint=fingerprint,
                    parsed=parsed,
                    anchor_dir=estate_anchor,
                    open_from=open_from,
                )
            )
        )
    except EngineError as exc:
        # start/resume gates (existing journal, hash/domain mismatch, live
        # socket): the run never started
        raise typer.Exit(refuse(exc)) from exc


def _observed_profile(
    staged: "StagedManifest | None", running_deadman: "float | None"
) -> "StagedManifest | None":
    """Re-pin the runtime profile on the deadman the run REALLY has.

    A reattaching engine meets a supervisor it did not start, and one
    already up cannot change its interval -- so `--deadman 90` against a
    supervisor running 60 gets 60, and the engine is started with 60. The
    manifest has to say 60 too: a profile that recorded the request would
    pin a number the estate does not have, which is what `_running_deadman`
    refuses to do for the routing table for the same reason (DL-126)."""
    from dsl41.period import RuntimeProfile, runtime_hash, to_us

    if staged is None:
        return None
    observed = None if running_deadman is None else to_us(running_deadman)
    if observed == staged.runtime_profile.deadman_us:
        return staged
    profile = RuntimeProfile.model_validate(
        {**staged.runtime_profile.model_dump(), "deadman_us": observed}
    )
    return staged.model_copy(
        update={"runtime_profile": profile, "runtime_hash": runtime_hash(profile)}
    )


def _active_period(run_root: Path) -> int:
    """Which period this root's ACTIVE segment holds (period-model I1).

    1 on a root that has never sealed. A reader that
    defaulted to 1 after a boundary would read period 1's manifest beside
    period N's records -- and a ROLLED root has no period 1 at all."""
    from dsl41.runner_history import RunHistoryError, active_period_id

    try:
        return active_period_id(run_root)
    except RunHistoryError:
        from dsl41.period import GENESIS_PERIOD_ID

        return GENESIS_PERIOD_ID


def _resume_profile_error(
    run_root: Path, profile: "RuntimeProfile", running_deadman: "float | None"
) -> "str | None":
    """PR-22's runtime half (period-model ss2.1): a period's semantics are
    (catalog_hash, runtime_hash, state_machine_version), and either of the
    first two moving is a new period. The catalog gate lives in resume;
    this holds the LAUNCH OPTIONS to the pin -- a resume that quietly
    rebuilt the adapters and the scheduler under different options would
    change period semantics with every identity gate green. A root with no
    manifest predates DL-130 and has no pin to hold. The deadman compares
    at its OBSERVED value, for the reason `_observed_profile` gives."""
    from dsl41.period import RuntimeProfile, read_period_manifest, runtime_hash, to_us
    from dsl41.runner_clock import EngineError

    try:
        # the ACTIVE period's manifest, never period 1's: every artifact
        # under `periods/` is addressed by the period number, and a rolled
        # root holds only the period it was opened into (DL-134)
        manifest = read_period_manifest(run_root, _active_period(run_root))
    except EngineError as exc:
        return str(exc)
    if manifest is None:
        return None
    observed_deadman = None if running_deadman is None else to_us(running_deadman)
    observed = RuntimeProfile.model_validate(
        {**profile.model_dump(), "deadman_us": observed_deadman}
    )
    if runtime_hash(observed) == manifest.runtime_hash:
        return None
    pinned = manifest.runtime_profile
    moved = sorted(
        name
        for name in type(observed).model_fields
        if getattr(observed, name) != getattr(pinned, name)
    )
    return (
        "runtime-profile mismatch: this resume was launched with different"
        f" options than the period pinned ({', '.join(moved) or 'runtime_hash'})."
        " A runtime-profile change is a new period (period-model ss2.1):"
        " re-baseline explicitly with a fresh run root"
    )


def _running_deadman(client: object, asked: "float | None", run_root: Path) -> "float | None":
    """The deadman the LOCAL SUPERVISOR reports it runs (concurrency-model
    ss8), and a warning when that is not what this invocation asked for.

    Read back rather than assumed, because the eviction bound has to describe
    the host: a reattaching engine meets a supervisor it did not start, and a
    supervisor already up cannot change its interval without being stopped.
    Silently recording the flag instead would put a number in the routing
    table that names nothing -- and that number is the length of the wait
    between an operator and a double run."""
    running = getattr(client, "supervisor_deadman_s", None) if client is not None else None
    if asked is not None and running != asked:
        typer.echo(
            f"note: the supervisor serving {run_root} runs deadman {running!r}, not the"
            f" {asked} asked for -- it was already up. Stop it"
            " (`dsl41 supervise shutdown`) to change the interval.",
            err=True,
        )
    return running


async def _serve_run(
    catalog: CatalogIR,
    run_root: Path,
    resume: bool,
    warns: list,
    *,
    profile: "RuntimeProfile",
    ui: bool = False,
    spec_texts: "dict[str, str] | None" = None,
    estate_fingerprint: "dict[str, str] | None" = None,
    parsed: "list[JilFile] | None" = None,
    anchor_dir: "Path | None" = None,
    open_from: "Path | None" = None,
) -> int:
    """`dsl41 run`, from the acquire to the last teardown.

    The profile is REQUIRED and is the ONE source of every launch option
    this function reads: timezone, alias table, tethered-vs-detached, the
    asked deadman, every adapter window. A `detached` flag beside a profile
    saying `tethered` would wire a supervised adapter and then tear down as
    if no supervisor existed -- DL-137's divergence, one level up."""
    import asyncio
    import contextlib
    import signal as signal_mod

    from datetime import datetime

    from dsl41.boundary import stage_period
    from dsl41.period import RuntimeProfile, to_us
    from dsl41.runner_startup import start_run, wire_from_profile
    from dsl41.runner_control import ControlServer
    from dsl41.runner_startup import resume_run as _resume_run
    from dsl41.runner_clock import EngineError, RealClock

    from dsl41.runner_ledger import acquire_run_root

    clock = RealClock()
    detached = profile.execution_mode == "detached"
    # the ASKED deadman; `_running_deadman` reads back what the host runs
    deadman = None if profile.deadman_us is None else profile.deadman_us / 1_000_000
    if open_from is not None:
        # the roll's READ-ONLY preflight runs before anything is created:
        # a refusal -- the unattested-closing refusal above all -- must
        # write nothing, not even the target directory and its lock. Every
        # gate re-runs authoritatively inside the roll under the locks.
        from dsl41.estate import check_roll_ready

        try:
            check_roll_ready(run_root, Path(open_from))
        except EngineError as exc:
            return refuse(exc)
    # ACQUIRE first (S6a, concurrency-model ss7). Earlier than the engine's
    # own entry points would, because the next thing this function does is
    # START a supervisor and take its lease -- an act on an estate this
    # process may turn out not to lead.
    try:
        lock = acquire_run_root(run_root)
    except EngineError as exc:
        return refuse(exc)
    # stage period 1 UNDER the lock (period-model ss1.1): a used run root is
    # start_run's refusal to make, and repainting `catalogs/` on the way to
    # that refusal is how the shipped binary used to write `manifest/` into
    # a root it turned out not to lead. What is left behind on a refusal is
    # content-addressed and never read -- residue the spec tolerates.
    # OWNERSHIP first, the FULL ss1.1 predicate: a sentinelless root that
    # keeps a WAL, a seal, a committed period or a populated runs/ is
    # somebody's work, and both the staging below and the supervisor start
    # after it are acts on an estate this process may turn out not to lead.
    if open_from is not None:
        # ss7's second opener, and its order is the whole argument:
        # new-root leader.lock (above), sentinel durable, anchor.lock and
        # the claim, the import, the segment, the head. What comes back is
        # an ordinary period-N root, and the ladder below resumes it --
        # there is no second semantic path (PR-07).
        from dsl41.estate import check_roll_target, roll_into_root

        try:
            check_roll_target(run_root, open_from)
            rolled = roll_into_root(
                run_root, anchor_dir=open_from, catalog_of=lambda _root, _m: catalog, lock=lock
            )
        except EngineError as exc:
            code = refuse(exc)
            lock.release()
            return code
        # stderr: stdout's first line is the `engine up` handshake every
        # supervisor and test reads, and a roll note ahead of it would move
        # the line they wait for
        typer.echo(
            f"opened period {rolled.seal.next_period.period_id} in {run_root} from seal"
            f" {rolled.seal.digest} ({rolled.closing_root}). This root's anchor is the"
            f" LINEAGE's: every later resume needs --estate-anchor {open_from}",
            err=True,
        )
        resume = True
    staged: "StagedManifest | None" = None
    if not resume:
        from dsl41.boundary import check_root_unused

        try:
            if not root_is_unused(run_root):
                raise EngineError(
                    f"{run_root}: already holds an estate -- genesis refuses a used"
                    " root; resume it (`dsl41 run --resume`) or pick a fresh one"
                    " (period-model ss1.1)"
                )
            check_root_unused(run_root)
        except EngineError as exc:
            code = refuse(exc)
            lock.release()
            return code
        if parsed is not None:
            staged = stage_period(run_root, parsed, catalog, profile)
    supervisor_deadman = deadman
    if resume:
        # start a MISSING supervisor with the deadman the period PINNED, not
        # the one this invocation asked for: asking 90 against a pinned 60
        # would otherwise start a 90-second supervisor before the profile
        # gate refuses -- and the next CORRECT 60-second resume then
        # observes 90 and refuses too. The ask still warns
        # (_running_deadman) and still refuses below if it differs; it just
        # never gets to reconfigure the host on the way to that refusal.
        from dsl41.period import read_period_manifest

        try:
            pinned = read_period_manifest(run_root, _active_period(run_root))
        except EngineError as exc:
            return refuse(exc)
        if pinned is not None:
            pinned_us = pinned.runtime_profile.deadman_us
            supervisor_deadman = None if pinned_us is None else pinned_us / 1_000_000
    # ONE wiring builder (DL-137): genesis, resume and the offline sealer
    # all build adapters and scheduler from the PROFILE, so a window that
    # moves on the pin moves in the components that run. The deadman is the
    # single field re-pinned here, for the reason above.
    pinned_deadman_us = None if supervisor_deadman is None else to_us(supervisor_deadman)
    try:
        wiring = await wire_from_profile(
            run_root,
            catalog,
            RuntimeProfile.model_validate(
                {**profile.model_dump(), "deadman_us": pinned_deadman_us}
            ),
        )
    except EngineError as exc:
        return refuse(exc)
    client = wiring.client
    adapters = wiring.adapters
    scheduler = wiring.scheduler
    running_deadman = _running_deadman(client, deadman, run_root)
    staged = _observed_profile(staged, running_deadman)
    if resume:
        error = _resume_profile_error(run_root, profile, running_deadman)
        if error is not None:
            return refuse(error)
        engine = await _resume_run(
            catalog,
            run_root,
            clock=clock,
            adapters=adapters,
            scheduler=scheduler,
            hold_open=True,
            supervisor=client,
            deadman_s=running_deadman,
            lock=lock,
            anchor_dir=anchor_dir,
        )
    else:
        engine = start_run(
            catalog,
            run_root,
            clock=clock,
            adapters=adapters,
            scheduler=scheduler,
            hold_open=True,
            deadman_s=running_deadman,
            lock=lock,
            staged=staged,
            anchor_dir=anchor_dir,
        )
    if client is not None:
        # ss8's supervisor clauses at the seal (PR-27): the boundary needs
        # the CLIENT to prove the LIST it reconciles came from the leased
        # incarnation, so the engine holds it, not just the adapter.
        engine.supervisor = client
        # ss8's "positive contact with this host": every confirmed lease
        # exchange from here on stamps the routing row (S5b). Wired after the
        # engine exists, which is why the first ACQUIRE above does not -- the
        # genesis seed stamps that same instant anyway.
        client.on_contact = engine.note_executor_contact
        # ss8: a host the leader cannot reach is quarantined, so new work is
        # HELD until it answers rather than failing against a supervisor that
        # is not there. The reinstate rides on the next confirmed contact.
        client.on_unreachable = engine.note_executor_unreachable
    # everything resume did not apply: E9's missed scheduler ticks, plus any
    # reconciliation completion the ss4 gate rejected. Both are on `drops`
    # (DL-91 finding 4 declined splitting them); the wording no longer claims
    # they are only the tick sweep.
    for ev, reason in engine.drops:
        typer.echo(f"dropped {ev.kind} {ev.job() or ''} @ {ev.at.isoformat()}: {reason}", err=True)
    if warns and engine.journal is not None:
        engine.journal.preflight(warns)
    server = ControlServer(
        engine,
        run_root / "control.sock",
        spec_texts=spec_texts,
        estate_fingerprint=estate_fingerprint,
    )
    try:
        await server.start()
    except EngineError as exc:
        return refuse(exc)
    typer.echo(f"engine up; control socket: {server.path}")
    loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError):
            # non-main-thread embedding (test harnesses): stoppable only by
            # engine failure; the real CLI always has the main thread
            pass
    stop_task = asyncio.ensure_future(stop.wait())
    ui_task: asyncio.Task | None = None
    tui = None
    if ui:
        from dsl41.runner_tui import RunnerApp

        # same terminal, same loop, still a client of the socket ONLY (ss11)
        tui = RunnerApp(server.path)
        ui_task = asyncio.ensure_future(tui.run_async())
    waiters = {loop_task, stop_task} | ({ui_task} if ui_task is not None else set())
    done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    stop_task.cancel()
    tui_exc: BaseException | None = None
    if ui_task is not None and ui_task in done and not ui_task.cancelled():
        tui_exc = ui_task.exception()  # a TUI crash is not an operator stop
    if tui is not None and ui_task is not None and ui_task not in done:
        tui.exit()  # engine crash or signal: detach the viewer first
        with contextlib.suppress(Exception):
            await ui_task
    # detach-stop (spec ss3 case b): teardown must NOT kill jobs -- the flag
    # makes the SupervisedCommandAdapter abandon its await instead of signaling.
    # Set before any adapter-task cancel; in-run oracle kills already happened
    # while the loop ran (stopping was False then).
    if detached:
        engine.detach.stopping = True
    code = 0
    sealed = loop_task in done and isinstance(loop_task.exception(), PeriodSealed)
    if sealed:
        # ss7: a committed boundary is a SUCCESSFUL terminal outcome, and
        # its code is its own -- distinct from 0/1/2, so an init system does
        # not restart-loop a sealed engine, and distinct from the crash
        # branch below, which `hold_open` makes the only other way this loop
        # can return. Detached work is NOT signalled: `detach.stopping` is
        # already set above, so the supervised adapter abandons its await
        # instead of killing a run the next period will reattach (PR-30b).
        typer.echo(str(loop_task.exception()))
        say_next(run_root, anchor_dir)
        code = 3
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, PeriodSealed):
            await loop_task
    elif loop_task in done:  # hold_open never quiesces: this is a crash
        typer.echo(f"engine failed: {loop_task.exception()}", err=True)
        code = 1
    else:
        # operator stop: a signal, or quitting the attached TUI (ss11 --ui
        # tethers the run to this terminal; viewers that must not stop the
        # run attach with `dsl41 ui` instead)
        if tui_exc is not None:
            typer.echo(f"TUI failed: {tui_exc!r}", err=True)
            code = 1
        if detached:
            typer.echo("stopping: jobs continue under the supervisor (detached, ss6a)")
        else:
            typer.echo("stopping: cancelling live jobs (wrappers record the kills, ss6a)")
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task
    await server.close()
    await engine.shutdown()
    if detached and client is not None:
        await client.release()
        await client.close()
        typer.echo(
            f"detached: reattach with `dsl41 run --resume --detached --run-root {run_root} <files>`"
        )
    if engine.journal is not None:
        engine.journal.close()
    return code


def rehearse(
    files: list[Path] = typer.Argument(..., help="JIL files forming the estate to rehearse"),
    scenario: Path = typer.Option(
        None,
        "--scenario",
        help="JSON scenario: adapter script + events to inject (see command help).",
    ),
    start: str = typer.Option(
        None, "--start", help="Virtual clock start, ISO datetime (default: wall now, UTC)."
    ),
    hours: float = typer.Option(
        24.0, "--hours", help="Horizon: quiesce once no work remains within start + HOURS."
    ),
    timezone: str = TIMEZONE_OPT,
    timezone_map: Path = TIMEZONE_MAP_OPT,
    run_root: Path = typer.Option(
        None, "--run-root", help="Also persist a WAL journal under this directory."
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Rehearse the estate under the virtual clock (runner-design ss9): the
    same engine path as `run` with scripted adapters, so a 24h estate plays
    in seconds and the printed trace is evidence about production behavior.

    Scenario file shape (all keys optional):
    {"adapter": {"default": [duration_s, exit_code] | null,
                 "runs": [{"job": J, "run_number": N,
                           "duration_s": S, "exit_code": C}, ...]},
     "events": [{"at": ISO, "kind": KIND, "payload": {...}}, ...]}
    -- events reuse the oracle trace tests' event shape; a null adapter
    default parks unscripted runs (the script drives completions).
    """
    import asyncio
    import json as json_mod

    from datetime import UTC, datetime, timedelta

    from dsl41.boundary import stage_period
    from dsl41.oracle_state import Event, OracleError
    from dsl41.period import runtime_profile_from_cli
    from dsl41.runner import Engine
    from dsl41.runner_startup import start_run
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import EngineError, VirtualClock
    from dsl41.runner_scheduler import Scheduler

    catalog, parsed, _ = load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)
    start_dt = (
        _naive_utc_arg(start, "--start")
        if start
        else datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    )
    tz_aliases = load_tz_aliases(timezone_map)
    warns = _preflight_or_exit(catalog, execution=False, start=start_dt, tz_aliases=tz_aliases)
    check_base_tz(timezone, tz_aliases)
    script: dict[tuple[str, int], tuple[float, int]] = {}
    default: tuple[float, int] | None = (0.0, 0)
    events: list[Event] = []
    if scenario is not None:
        try:
            data = json_mod.loads(scenario.read_bytes())
            adapter_spec = data.get("adapter", {})
            if "default" in adapter_spec:
                raw = adapter_spec["default"]
                default = None if raw is None else (float(raw[0]), int(raw[1]))
            for entry in adapter_spec.get("runs", []):
                key = (str(entry["job"]), int(entry["run_number"]))
                script[key] = (float(entry["duration_s"]), int(entry["exit_code"]))
            events = [Event.model_validate(entry) for entry in data.get("events", [])]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise typer.Exit(refuse(exc, prefix=f"scenario {scenario}")) from exc
    clock = VirtualClock(start_dt)
    adapter = FakeAdapter(script, default=default)
    scheduler = Scheduler(catalog, start=start_dt, default_tz=timezone, tz_aliases=tz_aliases)
    adapters = {"CMD": adapter, "FW": adapter}
    try:
        if run_root is not None:
            # a rehearsal's run root is a self-contained artifact like a
            # real one: the profile it interpreted the estate under -- its
            # timezone above all -- belongs in the manifest, or the log
            # claims a period it did not run (ss2.1). Staged only for a
            # FRESH root: an existing journal is start_run's refusal to
            # make, and nothing is written on the way to it.
            staged = (
                stage_period(
                    run_root,
                    parsed,
                    catalog,
                    runtime_profile_from_cli(timezone=timezone, tz_aliases=tz_aliases),
                )
                if root_is_unused(run_root)
                else None
            )
            engine = start_run(
                catalog,
                run_root,
                clock=clock,
                adapters=adapters,
                scheduler=scheduler,
                staged=staged,
            )
        else:
            engine = Engine(catalog, clock=clock, adapters=adapters, scheduler=scheduler)
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc
    if warns and engine.journal is not None:
        engine.journal.preflight(warns)
    for ev in events:
        engine.inject(ev, source="control")
    horizon = start_dt + timedelta(hours=hours)

    async def _play() -> None:
        try:
            await engine.run_until_quiescent(horizon)
        finally:
            await engine.shutdown()

    try:
        asyncio.run(_play())
    except (EngineError, OracleError) as exc:
        typer.echo(f"rehearse failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        if engine.journal is not None:
            engine.journal.close()
    for entry in engine.oracle.trace():
        typer.echo(f"{entry.at.isoformat()} {entry.job} {entry.transition} [{entry.cause}]")


# ------------------------------------------------- reading a run back


def journal(
    journal_file: Path = typer.Argument(
        ...,
        help="Run journal to replay: an estate root, its journal.jsonl sentinel, or a"
        " wal/NNNNNN.jsonl segment. A root or a sentinel resolves to the ACTIVE"
        " segment (the current period); name an earlier wal/NNNNNN.jsonl to replay"
        " an earlier one.",
    ),
    files: list[Path] = typer.Argument(..., help="JIL files forming the catalog the run used"),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Replay a run journal's inputs through a fresh Oracle and print the
    reconstructed trace.

    The WAL is inputs-only (runner-design ss7): emitted events and the trace
    are pure functions of the input sequence, so they are derived here, never
    stored. Refuses on catalog-hash mismatch -- a changed estate re-baselines
    explicitly.
    """
    from dsl41.oracle import Oracle
    from dsl41.oracle_state import OracleError
    from dsl41.period import catalog_hash_for, opening_at
    from dsl41.runner_clock import EngineError
    from dsl41.runner_hosts import LOCAL_EXECUTOR_ID, seed_local_executor
    from dsl41.runner_journal import read_journal, replay_inputs

    catalog = load_catalog_or_exit_2(files, permit_unknown, properties)
    try:
        records = read_journal(journal_file)
    except (OSError, EngineError) as exc:
        raise typer.Exit(refuse(exc)) from exc
    opening = records[0]
    # like for like (period-model ss1.1): the recipe is the one the
    # `segment` itself pins, never the one this build happens to write
    if opening.get("catalog_hash") != catalog_hash_for(opening, catalog):
        typer.echo(
            "catalog hash mismatch: the estate differs from the one this journal ran"
            " (runner-design ss7: no silent semantic drift)",
            err=True,
        )
        raise typer.Exit(2)
    oracle = Oracle(catalog)
    # reproducing a log means reproducing the genesis the engine replayed it
    # onto, not only the catalog: a routing-table input lands on a table that
    # already holds this engine's own executor (concurrency-model ss8), and a
    # replay without it would decide "no such host" where the run decided
    # otherwise. The stamp only reaches `last_contact`, which no input reads.
    seed_local_executor(oracle.store, LOCAL_EXECUTOR_ID, at=opening_at(opening))
    try:
        replay_inputs(oracle, records)
    except OracleError as exc:
        raise typer.Exit(refuse(exc, prefix="replay failed")) from exc
    for entry in oracle.trace():
        typer.echo(f"{entry.at.isoformat()} {entry.job} {entry.transition} [{entry.cause}]")


class RunsFormat(str, Enum):
    table = "table"
    json = "json"
    csv = "csv"


def _runs_table(rows: list[RunRow]) -> list[str]:
    """table format: fixed-width columns, sorted (job, started_at) by the
    caller already, plus a labelled break wherever the SAME job's rows cross a
    definition change (`runner_history` decision 4 -- never a hidden line,
    never a refusal to print)."""
    header = (
        f"{'JOB':<32} {'RUN':>4} {'STARTED_AT':<26} {'DURATION_S':>10}"
        f" {'STATUS':<11} {'CLOCK':<7} {'HASH':<10} BOX"
    )
    from dsl41.runner_history import definition_change

    lines = [header]
    previous: RunRow | None = None
    for row in rows:
        if previous is not None and previous.job == row.job:
            change = definition_change(previous, row)
            if change == "definition":
                lines.append(
                    f"  -- {row.job}: definition changed"
                    f" {previous.job_hash[:10] if previous.job_hash else '?'} ->"
                    f" {row.job_hash[:10] if row.job_hash else '?'} --"
                )
            elif change == "catalog":
                lines.append(
                    f"  -- {row.job}: catalog changed {previous.catalog_hash[:10]} ->"
                    f" {row.catalog_hash[:10]} (estate-wide: no per-job fingerprint here) --"
                )
        duration = "-" if row.duration_s is None else f"{row.duration_s:.1f}"
        lines.append(
            f"{row.job:<32} {row.run_number:>4} {row.started_at.isoformat():<26}"
            f" {duration:>10} {row.status:<11} {row.clock_source:<7}"
            f" {row.catalog_hash[:10]:<10} {row.box_name or '-'}"
        )
        previous = row
    return lines


def runs(
    run_roots: list[Path] = typer.Argument(
        ..., help="One or more run roots (dsl41 run --run-root TARGET)."
    ),
    job: str = typer.Option(None, "--job", help="Filter to one job's rows."),
    since: str = typer.Option(
        None, "--since", help="ISO 8601: only runs started at or after this instant."
    ),
    output_format: RunsFormat = typer.Option(
        RunsFormat.table,
        "--format",
        help="table (default): human-readable, with a labelled break at every"
        " catalog change. json / csv: every field, self-describing via catalog_hash"
        " on every row -- segment yourself by watching it change.",
    ),
) -> None:
    """Run history (DL-113): one row per job run, folded from each run
    root's journal + manifest + spool -- "how long did it take, run after
    run, and did it change." Offline only: no control socket, no live engine,
    and deliberately not a control-protocol verb (docs/control-protocol.md
    stays frozen at v2).

    Multiple run roots on one command line is the point: every row sorts by
    (job, started_at) across ALL of them, so a series that crosses a baseline
    change comes back segmented rather than blended into one misleading
    line -- never silently, and never refused."""
    from datetime import UTC
    from datetime import datetime as datetime_mod

    from dsl41.runner_history import RunHistoryError, read_run_roots

    since_at = None
    if since is not None:
        try:
            since_at = datetime_mod.fromisoformat(since)
        except ValueError as exc:
            raise typer.Exit(refuse(exc, prefix="--since")) from exc
        if since_at.tzinfo is not None:  # journal timestamps are naive UTC
            since_at = since_at.astimezone(UTC).replace(tzinfo=None)
    try:
        rows = read_run_roots(run_roots, job=job, since=since_at)
    except RunHistoryError as exc:
        raise typer.Exit(refuse(exc)) from exc

    if any(row.fidelity == "records_only" for row in rows):
        # Loud on stderr as well as on the row, because the one degraded
        # field that MISLEADS rather than omits is `status`.
        typer.echo(
            "warning: some run roots have no stored inputs, so their rows are"
            " fidelity=records_only: no box rows, no box_name/started_by/job_hash,"
            " and a run closed by KILLJOB or term_run_time reads as RUNNING",
            err=True,
        )

    if output_format is RunsFormat.json:
        import json as json_mod

        typer.echo(
            json_mod.dumps([row.model_dump(mode="json") for row in rows], indent=2, sort_keys=True)
        )
    elif output_format is RunsFormat.csv:
        import csv as csv_mod
        import io

        from dsl41.runner_history import RunRow as _RunRow

        fields = list(_RunRow.model_fields.keys())
        buf = io.StringIO()
        writer = csv_mod.writer(buf)
        writer.writerow(fields)
        for row in rows:
            dump = row.model_dump(mode="json")
            writer.writerow(["" if dump[f] is None else dump[f] for f in fields])
        typer.echo(buf.getvalue(), nl=False)
    else:
        for line in _runs_table(rows):
            typer.echo(line)
    raise typer.Exit(0)
