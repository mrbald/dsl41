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
    resume_target_period,
    say_next,
    walk_estate_or_exit_2,
)
from dsl41.ir import CatalogIR
from dsl41.period import root_is_unused

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from dsl41.boundary import EstateWalk
    from dsl41.oracle_state import Event, TraceEntry
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.period import RuntimeProfile, StagedManifest
    from dsl41.runner import Engine
    from dsl41.runner_history import RunRow
    from dsl41.runner_startup import Wiring
    from dsl41.seal import CarriedRows


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
    warns_to_stderr: bool = False,
) -> list:
    """Print ss8 findings; exit 2 on any ERROR; return the WARNs (the caller
    journals them next to the run -- WARN prints, journals, and runs).
    `start` anchors the DL-56 calendar-exhaustion WARN: run passes wall-now,
    rehearse its virtual --start. `warns_to_stderr` moves the WARN lines off
    stdout -- rehearse --format json owns stdout as ONE parseable document,
    and a WARN printed ahead of it broke every `| jq` (DL-180 review)."""
    from dsl41.runner_preflight import MachinePolicy, preflight

    if machine_policy not in ("strict", "local-eligible"):
        raise typer.Exit(
            refuse(f"--machine-policy {machine_policy!r}: expected strict|local-eligible")
        )
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
            err=warns_to_stderr or item.severity == "ERROR",
        )
    if any(item.severity == "ERROR" for item in items):
        raise typer.Exit(refuse("preflight: refusing to run (runner-design ss8)"))
    return items


def _naive_utc_arg(text: str, option: str) -> "datetime":
    from datetime import datetime

    from dsl41.canon import naive_utc

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise typer.Exit(refuse(exc, prefix=option)) from exc
    return naive_utc(parsed)


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
    access_map: Path = typer.Option(
        None,
        "--access-map",
        help="Role map arming the three-tier perimeter on the control socket"
        " (docs/access-model.md): strict TOML, principal -> tier. A configured"
        " path that is missing or invalid REFUSES startup; omit the option and"
        " the 0600 owner-only model stands unchanged. SIGHUP reloads the map.",
    ),
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
            raise typer.Exit(
                refuse(
                    "--open-from and --resume are the two OPENERS and you get one:"
                    " --resume continues the lineage in this root, --open-from opens the"
                    " next period into a fresh one (period-model ss7)"
                )
            )
        if estate_anchor is not None and Path(estate_anchor) != Path(open_from):
            raise typer.Exit(
                refuse(
                    f"--open-from {open_from} IS the lineage anchor; --estate-anchor"
                    f" {estate_anchor} names another one (period-model ss7)"
                )
            )
        estate_anchor = open_from
    catalog, parsed, fingerprint = load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)
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
        raise typer.Exit(refuse("--deadman needs --detached: a tethered run has no supervisor"))
    if deadman is not None and deadman <= 0:
        raise typer.Exit(refuse("--deadman must be a positive number of seconds"))
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
                    access_map=access_map,
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
    from dsl41.period import runtime_hash, to_us

    if staged is None:
        return None
    observed = None if running_deadman is None else to_us(running_deadman)
    if observed == staged.runtime_profile.deadman_us:
        return staged
    # the staged manifest carries BOTH the profile and its hash, so a
    # re-pinned deadman has to move the hash with it -- the two wiring sites
    # below take the profile alone and hash nothing
    profile = staged.runtime_profile.with_deadman(observed)
    return staged.model_copy(
        update={"runtime_profile": profile, "runtime_hash": runtime_hash(profile)}
    )


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
    from dsl41.period import (
        disagreements,
        read_period_manifest,
        runtime_hash,
        to_us,
    )
    from dsl41.runner_clock import EngineError

    try:
        # the manifest of the period this resume will OPEN INTO, never
        # period 1's: every artifact under `periods/` is addressed by the
        # period number, a rolled root holds only the period it was opened
        # into (DL-134), and a root with a committed boundary is about to
        # move to the next one (DL-151)
        manifest = read_period_manifest(run_root, resume_target_period(run_root))
    except EngineError as exc:
        return str(exc)
    if manifest is None:
        return None
    observed_deadman = None if running_deadman is None else to_us(running_deadman)
    observed = profile.with_deadman(observed_deadman)
    if runtime_hash(observed) == manifest.runtime_hash:
        return None
    pinned = manifest.runtime_profile
    # names only: the caller reports WHICH options moved, and the walk is
    # `period.disagreements` like every other artifact comparison (DL-137)
    moved = sorted(
        name for name, _, _ in disagreements(observed, pinned, type(observed).model_fields)
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
    access_map: "Path | None" = None,
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

    from datetime import UTC, datetime

    from dsl41.boundary import stage_period
    from dsl41.period import to_us
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
    if access_map is not None:
        # the access-model ss4 refusal must write NOTHING (the check_roll_ready
        # precedent above): the same load_policy that arming runs later is
        # called here read-only, before the root is claimed or the WAL opened.
        # `socket_group` resolution is the loader's own (DL-152), so an
        # unknown group refuses here too, and in the loader's words
        from dsl41.runner_access import AccessError, load_policy

        try:
            load_policy(access_map, generation=1)
        except AccessError as exc:
            return refuse(exc)
    # ACQUIRE first (S6a, concurrency-model ss7). Earlier than the engine's
    # own entry points would, because the next thing this function does is
    # START a supervisor and take its lease -- an act on an estate this
    # process may turn out not to lead.
    try:
        lock = acquire_run_root(run_root)
    except EngineError as exc:
        return refuse(exc)
    # ONE teardown, for every way out (DL-145). The supervisor's lease
    # and the leader lock are taken here and are this function's to give
    # back: the early refusals below used to return past a started
    # supervisor, leaving a 60-second lease and a held lock behind for a
    # retry that then refused for the wrong reason. The ORDER is the one
    # the normal path always had -- lease, then journal, then lock -- and
    # `release` is idempotent, so the S6a close inside `journal.close`
    # stays exactly where the log ends. A lease taken and then abandoned
    # INSIDE `wire_from_profile` is that function's own to give back; this
    # one can only close what it was handed.
    wiring: "Wiring | None" = None
    engine: "Engine | None" = None
    try:
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
                return refuse(exc)
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
                return refuse(exc)
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
                pinned = read_period_manifest(run_root, resume_target_period(run_root))
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
                profile.with_deadman(pinned_deadman_us),
                start=datetime.now(UTC).replace(tzinfo=None),
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
                # the launch options no wired object can report -- the
                # machine identity and its policy. Without them the core
                # gate inherits them from the pin and can never disagree
                # with it, which is how a boundary that staged a new
                # identity opened under the old one (DL-151)
                declared=profile,
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
            typer.echo(
                f"dropped {ev.kind} {ev.job() or ''} @ {ev.at.isoformat()}: {reason}", err=True
            )
        if warns and engine.journal is not None:
            engine.journal.preflight(warns)
        access = None
        if access_map is not None:
            # access-model ss4: a configured path that does not load REFUSES;
            # it never falls back to owner-wide authority
            from dsl41.runner_access import AccessControl, AccessError

            try:
                access = AccessControl.arm(access_map, run_root)
            except AccessError as exc:
                return refuse(exc)
        server = ControlServer(
            engine,
            run_root / "control.sock",
            spec_texts=spec_texts,
            estate_fingerprint=estate_fingerprint,
            access=access,
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
        if access is not None:
            # access-model ss7: explicit reload; a failed one keeps the old
            # policy and writes the receipt, never kills the engine
            try:
                loop.add_signal_handler(signal_mod.SIGHUP, access.reload)
            except (NotImplementedError, ValueError):
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
        if client is not None:
            # a client exists exactly when the profile said detached, so the
            # second half of the guard this replaced tested nothing; the
            # lease itself is given back in the one teardown below
            typer.echo(
                f"detached: reattach with `dsl41 run --resume --detached --run-root {run_root} <files>`"
            )
        return code
    finally:
        # EACH step guaranteed, whatever the one before it did (DL-145).
        # These are three obligations to three different things -- the
        # supervisor's lease, the log's own fsync, the estate's leader lock
        # -- and none of them is the others' to skip. A failing fsync inside
        # `journal.close()` must PROPAGATE, because durability is not
        # suppressible here any more than it is in the liturgy; what it must
        # not ALSO do is strand the lock, which would refuse the operator's
        # own retry with "held by another engine" by a process that is gone.
        # A chain of statements gave the first raiser the power to skip the
        # rest; nesting takes it away and still lets it out.
        try:
            if wiring is not None:
                await wiring.close()
        finally:
            try:
                if engine is not None and engine.journal is not None:
                    engine.journal.close()
            finally:
                lock.release()


class RehearseFormat(str, Enum):
    """`rehearse --format` (arch-review 2026-08-28: one axis for what were
    two flags with a silently-ignored combination -- the DL-75 shape)."""

    text = "text"  # trace lines
    summary = "summary"  # trace lines + the per-job rollup
    json = "json"  # one document: trace + rollup


def _trace_line(entry: "TraceEntry") -> str:
    """The one spelling of a printed trace line -- rehearse and the journal
    replay used to carry a copy each (arch-review 2026-08-28)."""
    return f"{entry.at.isoformat()} {entry.job} {entry.transition} [{entry.cause}]"


def _scenario_adapter(scenario: Path | None) -> "tuple[FakeAdapter, list[Event]]":
    """The rehearse scenario file, parsed into what rehearse actually wires:
    the scripted adapter and the events to inject -- three of the old
    four-tuple's members were FakeAdapter's own ctor args (arch-review
    2026-08-28). The docstring on `rehearse` states the accepted shapes.
    Raises OSError/ValueError/TypeError/KeyError; the caller owns the
    refusal."""
    import json as json_mod

    from dsl41.oracle_state import Event
    from dsl41.runner_adapters import FakeAdapter

    script: dict[tuple[str, int], tuple[float, int] | None] = {}
    default: tuple[float, int] | None = (0.0, 0)
    park: frozenset[str] = frozenset()
    events: list[Event] = []
    if scenario is None:
        return FakeAdapter(script, default=default, park=park), events
    data = json_mod.loads(scenario.read_bytes())
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object at the top level")
    adapter_spec = data.get("adapter", {})
    if not isinstance(adapter_spec, dict):
        raise ValueError('"adapter": expected an object')
    if "default" in adapter_spec:
        raw = adapter_spec["default"]
        default = None if raw is None else _scenario_completion(raw, "adapter.default")
    park_raw = adapter_spec.get("park", [])
    if isinstance(park_raw, str) or not isinstance(park_raw, list):
        raise ValueError('"adapter.park": expected a list of job names')
    park = frozenset(str(j) for j in park_raw)
    for entry in adapter_spec.get("runs", []):
        if not isinstance(entry, dict) or "job" not in entry or "run_number" not in entry:
            raise ValueError('adapter.runs entries need "job" and "run_number"')
        key = (str(entry["job"]), int(entry["run_number"]))
        if entry.get("park", False):
            if "duration_s" in entry or "exit_code" in entry:
                raise ValueError(
                    f"adapter.runs {key[0]}#{key[1]}: park excludes duration_s/exit_code"
                )
            script[key] = None
        else:
            script[key] = _scenario_completion(entry, f"adapter.runs {key[0]}#{key[1]}")
    events = [Event.model_validate(entry) for entry in data.get("events", [])]
    return FakeAdapter(script, default=default, park=park), events


def _emit_rehearsal(
    trace: list[TraceEntry], catalog_jobs: Iterable[str], *, fmt: RehearseFormat
) -> None:
    """The rehearse report: the trace, and the per-job rollup -- a run is a
    transition INTO STARTING; the final status is the last transition's
    target (out-of-band markers carry no "->" and count for neither).
    Never-started catalog jobs appear with runs=0: "which job fired how
    often" is the question a rehearsal answers (DL-180 -- counting trace
    lines by hand was most of the reporting harness)."""
    import json as json_mod

    run_counts: dict[str, int] = {}
    final_status: dict[str, str] = {}
    for t in trace:
        _, arrow, new_status = t.transition.partition("->")
        if not arrow:
            continue
        final_status[t.job] = new_status
        if new_status == "STARTING":
            run_counts[t.job] = run_counts.get(t.job, 0) + 1
    names = sorted(set(catalog_jobs) | set(final_status))
    if fmt is RehearseFormat.json:
        doc = {
            "trace": [t.model_dump(mode="json") for t in trace],
            "jobs": {
                name: {"runs": run_counts.get(name, 0), "final_status": final_status.get(name)}
                for name in names
            },
        }
        typer.echo(json_mod.dumps(doc, sort_keys=True))
        return
    for t in trace:
        typer.echo(_trace_line(t))
    if fmt is RehearseFormat.summary:
        typer.echo("-- summary: runs per job --")
        for name in names:
            typer.echo(f"{name} runs={run_counts.get(name, 0)} final={final_status.get(name, '-')}")


def _scenario_completion(raw: object, where: str) -> tuple[float, int]:
    """One scenario completion, in BOTH spellings: the list
    `[duration_s, exit_code]` and the runs-entry object
    `{"duration_s": S, "exit_code": C}`. The default used to take only the
    list while runs entries were objects, and the object form died as a
    leaked `KeyError(0)` -- printed as `scenario file.json: 0` (2026-08-28
    feedback). Malformed shapes refuse with the two accepted forms named."""
    if isinstance(raw, dict):
        if "duration_s" not in raw or "exit_code" not in raw:
            raise ValueError(f"{where}: object form needs duration_s and exit_code")
        return float(raw["duration_s"]), int(raw["exit_code"])
    if isinstance(raw, list) and len(raw) == 2:
        return float(raw[0]), int(raw[1])
    raise ValueError(
        f'{where}: expected [duration_s, exit_code] or {{"duration_s": S, "exit_code": C}}'
    )


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
    output: RehearseFormat = typer.Option(
        RehearseFormat.text,
        "--format",
        help="text: trace lines; summary: trace + per-job run counts and final"
        " statuses; json: one document carrying both.",
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
    {"adapter": {"default": [duration_s, exit_code]
                     | {"duration_s": S, "exit_code": C} | null,
                 "park": [job, ...],
                 "runs": [{"job": J, "run_number": N,
                           "duration_s": S, "exit_code": C}
                          | {"job": J, "run_number": N, "park": true}, ...]},
     "events": [{"at": ISO, "kind": KIND, "payload": {...}}, ...]}
    -- events reuse the oracle trace tests' event shape. A null default
    parks EVERY unscripted run (the script drives completions); "park"
    parks every run of the named jobs -- a file watcher that must not fire
    during the rehearsal -- and a runs entry with park:true parks that one
    run. Precedence: runs entry, then park, then default.
    """
    import asyncio

    from datetime import UTC, datetime, timedelta

    from dsl41.boundary import stage_period
    from dsl41.oracle_state import OracleError
    from dsl41.period import runtime_profile_from_cli
    from dsl41.runner import Engine
    from dsl41.runner_startup import start_run
    from dsl41.runner_clock import EngineError, VirtualClock
    from dsl41.runner_scheduler import Scheduler

    catalog, parsed, _ = load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)
    start_dt = (
        _naive_utc_arg(start, "--start")
        if start
        else datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    )
    tz_aliases = load_tz_aliases(timezone_map)
    warns = _preflight_or_exit(
        catalog,
        execution=False,
        start=start_dt,
        tz_aliases=tz_aliases,
        warns_to_stderr=output is RehearseFormat.json,
    )
    check_base_tz(timezone, tz_aliases)
    try:
        adapter, events = _scenario_adapter(scenario)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise typer.Exit(refuse(exc, prefix=f"scenario {scenario}")) from exc
    clock = VirtualClock(start_dt)
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
    _emit_rehearsal(engine.oracle.trace(), catalog.jobs, fmt=output)


# ------------------------------------------------- reading a run back


def journal(
    journal_file: Path = typer.Argument(
        ...,
        help="Run journal to replay: an estate root, its journal.jsonl sentinel, or a"
        " wal/NNNNNN.jsonl segment. A root or a sentinel replays EVERY segment the"
        " root retains, in period order; name one wal/NNNNNN.jsonl to replay exactly"
        " that period. Name the lineage ANCHOR directory instead and the read is"
        " ESTATE-WIDE: every root the registry holds, in period order.",
    ),
    files: list[Path] = typer.Argument(
        None,
        help="JIL files forming the catalog the FIRST replayed period ran under."
        " OPTIONAL since DL-142: omitted, every period's catalog is loaded from the"
        " estate's own content-addressed bundle, by the hash that period's opening"
        " `segment` pins.",
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Replay a run journal's inputs through a fresh Oracle and print the
    reconstructed trace.

    The WAL is inputs-only (runner-design ss7): emitted events and the trace
    are pure functions of the input sequence, so they are derived here, never
    stored. Refuses on catalog-hash mismatch -- a changed estate re-baselines
    explicitly.

    **It CROSSES boundaries** (period-model ss11; DL-142). At each `segment`
    record the replay folds state through the seal exactly as an engine
    opening the period does -- `open_from_seal`, the one opener -- loads the
    next period's catalog from the bundle that segment pins, and continues.
    The boundary is narrated, never silent -- and only once it has been
    crossed. A boundary is crossed only over a seal that proves out: the
    digest the record names, the record's own fields against the sidecar
    (ss2.2), the chain, `next_period` agreement, and the seal RE-DERIVED
    from the period's own evidence -- or, for a later segment named alone,
    its predecessor's attestation. Anything less refuses by name, because a
    read-only replay across a forged seal would narrate a forged
    continuation just as confidently as a true one.

    **The catalog argument is optional and never wins over a pin.** The
    estate has held its own inputs since DL-130 and a bundle re-parses under
    the ORIGINAL paths `sources.json` records, so it reproduces the very
    `catalog_hash` the segment pins -- which is what lets this verb answer
    with no estate-file argument at all, as `dsl41 runs` already does. Files GIVEN are the FIRST replayed period's catalog and are
    hash-gated against its pin exactly as before; later periods still come
    from their own bundles, because one supplied catalog cannot be many
    periods' catalogs. Files OMITTED, every period including the first comes
    from its bundle. `--permit-unknown` and `-p` therefore govern the files
    a caller SUPPLIES and nothing else: a bundle is the bytes the period
    ran, already through the launch gate and already post-placeholder.

    **Pointed at the ESTATE it replays the whole lineage.** Given the anchor
    directory it walks ss1.3's archive registry -- every period, in period
    order, with the root that holds it -- so period 1 is found after a
    physical roll without knowing which root it went to (PR-02f), and the
    roll is crossed like any other boundary.
    """
    from dsl41.boundary import is_anchor_dir

    catalog = load_catalog_or_exit_2(files, permit_unknown, properties) if files else None
    if is_anchor_dir(journal_file):
        _replay_estate(walk_estate_or_exit_2(journal_file), catalog)
        return
    _replay_lineage(_segments_named(journal_file), catalog)


def _segments_named(target: Path) -> list[Path]:
    """Which segments the caller meant by one path.

    A ROOT or a sentinel means the whole root -- every period it retains, in
    order. That is the same correction DL-136 made to `dsl41 runs`: a reader
    that resolved a root to its ACTIVE segment was right while a root held
    one journal and became silently wrong the moment a root held one per
    period, answering about the current night and saying nothing about the
    nights before it. A `wal/NNNNNN.jsonl` names exactly one period and
    still replays exactly that one."""
    from dsl41.period import (
        archived_periods,
        estate_segments,
        resolve_wal,
        root_of_wal,
        wal_path,
    )

    resolved = resolve_wal(target)
    whole_root = Path(target).is_dir() or Path(target) != resolved
    if not whole_root:
        return [resolved]
    held = estate_segments(target)
    if not all(_periodized(path) for path in held):
        # a root with no periodized sentinel answers with `journal.jsonl`
        # itself, whose stem is not a period number. There is no lineage
        # here to have archived anything, and the reader below refuses it
        # by name -- which a numeric sort would have turned into a
        # traceback out of a diagnosis verb
        return list(held)
    # an ARCHIVED period retains no file and is still a period of this
    # root. Naming its segment here is what makes the replay ANNOUNCE the
    # unreplayable gap instead of answering with a shorter lineage that
    # looks whole (DL-144)
    root = root_of_wal(resolved)
    gone = {wal_path(root, number) for number in archived_periods(root)}
    gone |= {wal_path(root, number) for number in _lost_below(root, held)}
    return sorted({*held, *gone}, key=lambda path: int(path.stem))


def _lost_below(root: Path, held: "Sequence[Path]") -> list[int]:
    """Period numbers BELOW the oldest segment this root retains for which
    the root still holds a committed period manifest (DL-144).

    A root-local read has no registry to ask who owns a period, and this is
    the one local fact that answers it: genesis and every in-place opening
    write `periods/<N>/manifest.json` into the root that ran period N,
    while a ROLLED root imports only the successor's. So a manifest here
    with no segment and no receipt is THIS root's period, gone -- which
    `_announce_gap` then refuses as loss rather than replaying a lineage
    that starts later than it did.

    BELOW the oldest retained segment and nowhere else, because that is
    where an archive lives (the archived periods are a prefix, ss12a) and
    because a COMMITTED-NEXT period legitimately has a manifest and no
    segment yet -- and it is always above."""
    from dsl41.period import wrote_period

    if not held:
        return []
    oldest = min(int(path.stem) for path in held)
    return [number for number in range(1, oldest) if wrote_period(root, number)]


def _replay_estate(walk: "EstateWalk", catalog: "CatalogIR | None") -> None:
    """Every segment the registry names, in period order, replayed as one
    lineage.

    **The enumeration comes first and completes.** It is the estate-wide
    half: each period, the root that holds it and the segment file, whether
    or not the replay below reaches it. Replaying inside that loop put the
    whole listing behind one segment's gate, so a refused replay printed
    period 1's refusal and nothing else. What is named is exactly what is
    replayed -- the walk's rows, not the disk's -- so a segment the registry
    has not finalized cannot appear in one half and be missing from the
    other."""
    from dsl41.period import wal_path

    for entry in walk.periods:
        segment = wal_path(entry.root, entry.period_id)
        archived = ""
        if entry.archived is not None:
            # two different disks, two different words: the receipt says
            # the period is archived either way, and only the missing file
            # makes it unreplayable
            archived = (
                "  [inputs archived, still on disk]"
                if segment.is_file()
                else "  [inputs archived: unreplayable]"
            )
        typer.echo(f"period {entry.period_id} in {entry.root}: {segment}{archived}")
    # LABELLED even for a one-period lineage: an estate-wide answer names
    # the period it is about, and a walk of a lineage that has not rolled
    # yet is still an estate-wide answer
    _replay_lineage(
        [wal_path(entry.root, entry.period_id) for entry in walk.periods],
        catalog,
        labelled=True,
    )


def _label(segment: Path) -> str:
    """`period N in ROOT` for one segment file, read off the layout alone
    (I1: segment N is period N).

    Only a MANY-segment read is labelled, and every many-segment list comes
    from `estate_segments` or from the walk -- both of which spell
    `wal/NNNNNN.jsonl` and nothing else. A root with no `wal/` yields ONE
    file, which this never sees."""
    from dsl41.period import root_of_wal

    return f"period {int(segment.stem)} in {root_of_wal(segment)}"


def _replay_lineage(
    segments: "Sequence[Path]", catalog: "CatalogIR | None", *, labelled: bool = False
) -> None:
    """One lineage's segments, replayed as ONE run of the state machine.

    A period does not start from nothing and does not keep its predecessor's
    catalog. So each segment gets a fresh interpreter opened from the seal
    the segment names -- the same `open_from_seal` fold an engine performs
    (period-model ss7 phase 3) -- under the catalog its own bundle holds,
    and the traces concatenate. Crossing a boundary is therefore the spec's
    own opening, not a second way to do it.

    Every boundary is PROVED before it is crossed: the segment must sit
    where it says it does (`check_segment_identity`), the seal record that
    closes the older segment must be the one the newer opens from, with one
    estate and a continuous index frontier (`check_segment_adjacency`), and
    the sidecar on disk must be the seal that opening stands on
    (`prove_opening`). Refuse-don't-degrade applies to a diagnosis surface
    as much as to an engine (DL-139): a narrated continuation across a
    forged seal reads exactly like a true one.

    **The crossing is announced only once it has happened.** The period is
    OPENED first -- catalog and carry, both of which can refuse -- and the
    line prints after. Printed before, it stated as fact the very crossing
    the refusal on the next line denied. It goes to STDOUT with the trace
    and not to stderr, deliberately: a reader who dropped stderr would be
    handed two periods' transitions concatenated with no seam, which is the
    "shorter trace that looks whole" this unit exists to stop.

    `labelled` forces the per-period prefix on a read whose segments happen
    to number one -- an estate-wide walk of a lineage with a single period
    is still an estate-wide answer, and its refusals name the period."""
    from dsl41.period import root_of_wal
    from dsl41.runner_clock import EngineError
    from dsl41.runner_history import RunHistoryError, check_replay_version
    from dsl41.runner_journal import (
        check_segment_adjacency,
        check_segment_identity,
        check_segment_tail,
        read_journal,
    )

    labelled = labelled or len(segments) > 1
    previous: list[dict] | None = None
    previous_segment: Path | None = None
    # the caller's catalog gates the first period this read actually
    # REPLAYS, which is not list slot 0 once a gap can sit in front of it:
    # gating on the slot handed the first replayed period `None` and
    # skipped the hash check the argument exists for (DL-144 review)
    first = True
    for position, segment in enumerate(segments):
        where = _label(segment) if labelled else ""
        if _periodized(segment) and not segment.is_file():
            # an ARCHIVED period, or a lost one. `_announce_gap` refuses
            # unless a receipt licenses the absence, and otherwise prints
            # the gap and drops `previous`, which is what makes the NEXT
            # boundary cross by its predecessor's attestation instead of
            # by a re-derivation there is no evidence for (DL-142, DL-144).
            # A path that is NOT `wal/NNNNNN.jsonl` names no period and
            # falls through to `read_journal`, which refuses it by name
            _announce_gap(segment, where=where)
            previous, previous_segment = None, None
            continue
        try:
            records = read_journal(segment)
            if position < len(segments) - 1:
                # this reader knows a segment is CLOSED by its place in the
                # list it was given, which is before it replays the segment
                # -- so a torn tail refuses ahead of the trace rather than
                # after it (`check_segment_tail`: positioned per caller)
                check_segment_tail(segment, records)
            check_segment_identity(segment, records[0])
            # period-model ss2.1: a foreign state machine cannot lead OR
            # replay, and the resume-side gate never runs here. HERE and not
            # at the replay: the crossing below is proved, paid for and
            # announced first, and a refusal after that has already stated
            # as fact the crossing it then denies
            check_replay_version(records[0])
            if previous is not None:
                check_segment_adjacency(
                    previous, records, where=f"journal {previous_segment} -> {segment}"
                )
        except (OSError, EngineError, RunHistoryError) as exc:
            raise typer.Exit(refuse(exc, prefix=where)) from exc
        root = root_of_wal(segment)
        if records[0].get("opens_from_seal") is not None:
            _prove_crossing(
                root,
                records[0],
                predecessor=(None if previous_segment is None else root_of_wal(previous_segment)),
                where=where,
            )
        if _periodized(segment):
            _announce_archived(segment, where=where)
        # ss7 phase 3, offline: what this period opened with. Nothing is
        # printed between the two, so a boundary that cannot be opened is
        # never announced as opened.
        supplied = catalog if first else None
        opened = (
            _period_catalog(root, records[0], supplied, where=where),
            _period_carry(root, records[0], where=where),
        )
        first = False
        if previous is not None:
            typer.echo(
                f"period {previous[-1]['period_id']} sealed at index"
                f" {previous[-1]['closes_at_index']}; period"
                f" {records[0]['period_id']} opens in {root}"
            )
        _run_period(
            records, opened, where=where, tz_aliases=_period_aliases(root, records[0], where=where)
        )
        previous, previous_segment = records, segment


def _periodized(segment: Path) -> bool:
    """Whether this path is a `wal/<six digits>.jsonl` that NAMES a period.

    A root with no periodized sentinel resolves to `journal.jsonl` itself
    (period-model ss1.1, DL-138), and every archive question below is
    keyed on a period number that such a path does not have. Asked here so
    the readers can go on refusing it by name instead of raising out of an
    `int()` (DL-144 review).

    Answered by ROUND-TRIPPING the layout's own helpers rather than by
    spelling `wal/` inline (DL-145): a path is period N's segment exactly
    when the owner, given the root this path belongs to, names this path
    for N."""
    from dsl41.period import root_of_wal, wal_path

    if not segment.stem.isdigit():
        return False
    return wal_path(root_of_wal(segment), int(segment.stem)) == segment


def _announce_gap(segment: Path, *, where: str) -> None:
    """One archived period, named on STDOUT as an unreplayable gap
    (period-model ss12, DL-144).

    On stdout with the trace, on DL-142's rule: a reader who dropped
    stderr would otherwise be handed the periods on either side of the gap
    concatenated with no seam, which is the "shorter trace that looks
    whole" this verb exists to stop.

    A segment that is simply GONE is a different fact and refuses. The
    receipt is what separates the two, and it has to LICENSE this very
    file: a receipt that archived only a candidate pair does not excuse a
    WAL that went missing by accident."""
    from dsl41.attest import verify_archive_receipt
    from dsl41.period import archive_receipt_path, root_of_wal
    from dsl41.runner_clock import EngineError

    root = root_of_wal(segment)
    period_id = int(segment.stem)
    try:
        # the ONE door (attest.py): a receipt that does not prove out is
        # not a licence, and narrating a gap on its word would be this
        # verb reporting an archive where the estate has a hole
        receipt = verify_archive_receipt(root, period_id, licensing=segment)
    except (OSError, EngineError) as exc:
        raise typer.Exit(refuse(exc, prefix=where)) from exc
    if receipt is None:
        raise typer.Exit(
            refuse(
                f"{where}: {segment} is not there, and no archive receipt licenses its"
                f" absence (`{archive_receipt_path(root, period_id).name}`) -- this is"
                " LOSS and not an archive: retention writes the receipt before it deletes"
                " anything, exactly so the two can be told apart (period-model ss12)"
            )
        )
    typer.echo(
        f"period {period_id} in {root}: inputs archived"
        f" ({archive_receipt_path(root, period_id).name}) -- UNREPLAYABLE GAP, no"
        " trace for this period; it stands at the attestation-verified tier and the"
        " next boundary is crossed by that checkpoint (period-model ss11, ss12)"
    )


def _announce_archived(segment: Path, *, where: str) -> None:
    """An archived period whose segment is STILL on disk, named before it
    is replayed (DL-144).

    That state is the crash window between the receipt and the deletions,
    and it is also what a restored file looks like -- the archive is
    irreversible, so the period reads at the attestation-verified tier
    whatever is beside it. The inputs may still be READ, which is why this
    narrates rather than refuses; but a trace printed with no word about
    the tier would be the one output that disagrees with `audit`."""
    from dsl41.attest import verify_archive_receipt
    from dsl41.period import archive_receipt_path, root_of_wal
    from dsl41.runner_clock import EngineError

    root = root_of_wal(segment)
    period_id = int(segment.stem)
    try:
        receipt = verify_archive_receipt(root, period_id)
    except (OSError, EngineError) as exc:
        raise typer.Exit(refuse(exc, prefix=where)) from exc
    if receipt is None or not receipt.licenses(root, segment):
        return
    typer.echo(
        f"period {period_id} in {root}: inputs ARCHIVED"
        f" ({archive_receipt_path(root, period_id).name}) and this segment is still on"
        " disk -- the archive is irreversible, so the period stands at the"
        " attestation-verified tier; what follows is read from evidence the estate no"
        " longer stands behind (period-model ss11, ss12)"
    )


def _prove_crossing(root: Path, opening: dict, *, predecessor: "Path | None", where: str) -> None:
    """ss11's "verified means RE-DERIVED, not self-consistent", asked of the
    seal this period opens from -- before the period is opened and before
    the crossing is announced.

    Every other check on this path proves INTEGRITY and BINDING: the
    sidecar digests to what the record names, the record is what the
    opening names, the fields agree. A forger who rewrites the sidecar
    canonically, recomputes its digest and copies that digest into both the
    closing `seal` record and the successor's opening passes all of them,
    and the replay would then narrate state that no run ever produced. Two
    proofs close that, and which one is available is a fact about what this
    read is holding:

    * **the predecessor's evidence is being replayed** (the ordinary
      lineage walk, in place or across a roll) -- `attest.prove_derived`
      rebuilds the predecessor seal from the period's own WAL, spool and
      manifests, in the root that HOLDS them, and refuses when the stored
      sidecar is not what they produce. `rederive_seal` also runs
      `check_record_names_sidecar` on the way, so a rewritten `seal` RECORD
      over an honest sidecar refuses here too, naming the fields.
    * **a later segment was named ALONE** -- the predecessor's inputs are
      not being read, so there is nothing to re-derive from and the "the
      replay reads the inputs" argument that lets this verb cross without
      an attestation is simply false. What stands for a period whose
      evidence this read does not hold is the attestation (ss11), so it is
      required here and its absence is a refusal that names it.

    The cost is stated where it is paid: an unpruned lineage replays each
    period twice, once to re-derive its seal and once to narrate it."""
    from dsl41.attest import prove_derived, verify_attestation
    from dsl41.runner_clock import EngineError

    link = opening["opens_from_seal"]
    period_id = int(link["period_id"])
    try:
        if predecessor is None:
            verify_attestation(root, period_id)
        else:
            prove_derived(predecessor, period_id)
    except (OSError, EngineError) as exc:
        raise typer.Exit(refuse(exc, prefix=where)) from exc


def _period_aliases(root: Path, opening: "dict", *, where: str) -> "dict[str, str] | None":
    """This period's SEM-35 alias table, from its own pin (period-model
    ss2.1). None where the root no longer holds the manifest, which is the
    same degrade `_period_catalog` makes for the same reason."""
    from dsl41.period import read_period_manifest, tz_aliases_of
    from dsl41.runner_clock import EngineError

    try:
        manifest = read_period_manifest(root, int(opening["period_id"]))
    except EngineError as exc:
        raise typer.Exit(refuse(exc, prefix=where)) from exc
    return tz_aliases_of(None if manifest is None else manifest.runtime_profile)


def _run_period(
    records: list[dict],
    opened: "tuple[CatalogIR, CarriedRows | None]",
    *,
    where: str,
    tz_aliases: "dict[str, str] | None" = None,
) -> None:
    """ONE period, replayed and printed. ONE implementation: a
    single-segment read and a lineage that crosses four boundaries differ
    in which segments they name, never in what replaying one of them
    means."""
    from dsl41.oracle import Oracle
    from dsl41.oracle_state import OracleError
    from dsl41.period import opening_at
    from dsl41.runner_clock import EngineError
    from dsl41.runner_hosts import LOCAL_EXECUTOR_ID, seed_local_executor
    from dsl41.runner_journal import replay_inputs

    catalog, carried = opened
    named = f"{where}: " if where else ""
    # SEM-35: the period's own alias table (DL-151). A narration that
    # resolved `timezone:` without it refuses the log the engine wrote,
    # because a `ujo_timezones` name is site-local and lives in the pin.
    oracle = Oracle(catalog, carried=carried, tz_aliases=tz_aliases)
    # reproducing a log means reproducing the genesis the engine replayed it
    # onto, not only the catalog: a routing-table input lands on a table that
    # already holds this engine's own executor (concurrency-model ss8), and a
    # replay without it would decide "no such host" where the run decided
    # otherwise. The stamp only reaches `last_contact`, which no input reads.
    seed_local_executor(oracle.store, LOCAL_EXECUTOR_ID, at=opening_at(records[0]))
    try:
        replay_inputs(oracle, records)
    except (OracleError, EngineError) as exc:
        raise typer.Exit(refuse(exc, prefix=f"{named}replay failed")) from exc
    for entry in oracle.trace():
        typer.echo(_trace_line(entry))


def _period_catalog(
    root: Path, opening: dict, supplied: "CatalogIR | None", *, where: str
) -> CatalogIR:
    """This period's catalog: the caller's files when they gave any, else
    the estate's own bundle -- and in BOTH cases like for like against the
    hash the `segment` itself pins (period-model ss1.1), never the one this
    build happens to write.

    The two mismatches keep two texts on purpose. They send a reader to two
    different artifacts: a supplied catalog that disagrees is a checkout at
    the wrong revision, and a BUNDLE that disagrees is the estate's own
    stored bytes failing to rebuild what they ran as -- corruption, or a
    build whose lowering moved under a pin that did not.

    A bundle is loaded with `permit_unknown`, always, and not from the CLI
    flag: these are the exact bytes this period RAN, the gate that decided
    whether an unknown attribute was acceptable ran once at launch, and
    re-asking it here would make `dsl41 journal` refuse a root that `dsl41
    runs` answers about (`load_catalog_from_manifest` made the same call).
    The `catalog_hash` pin below is what proves like for like; the flag
    still governs the files a caller supplies."""
    from dsl41.boundary import load_bundle_catalog
    from dsl41.period import catalog_hash_for
    from dsl41.runner_clock import EngineError

    named = f"{where}: " if where else ""
    if supplied is None:
        try:
            catalog = load_bundle_catalog(
                root, str(opening["source_bundle_hash"]), permit_unknown=True
            )
        except EngineError as exc:
            raise typer.Exit(refuse(exc, prefix=where)) from exc
        if opening["catalog_hash"] != catalog_hash_for(opening, catalog):
            raise typer.Exit(
                refuse(
                    f"{named}the bundle {opening['source_bundle_hash']} does not reproduce"
                    " the catalog hash this segment pins -- the stored inputs are not the"
                    " ones this period ran (period-model ss1.1)"
                )
            )
        return catalog
    if opening["catalog_hash"] != catalog_hash_for(opening, supplied):
        raise typer.Exit(
            refuse(
                f"{named}catalog hash mismatch: the estate differs from the one this"
                " journal ran (runner-design ss7: no silent semantic drift)"
            )
        )
    return supplied


def _period_carry(root: Path, opening: dict, *, where: str) -> "CarriedRows | None":
    """The rows this period OPENED with, or None for period 1.

    `attest.carried_from_opening` is the one derivation of that fact and
    `open_from_seal` is the one opener; this adds only the ss11 proofs a
    reader owes before it trusts a sidecar it did not write. A period
    replayed from an EMPTY oracle derives revisions and run numbers the log
    never recorded and refuses at the first admitted input that touches a
    carried entity (DL-136) -- so a later segment named ALONE was already
    unreplayable, and this is what makes the command DL-141 printed work."""
    from dsl41.attest import carried_from_opening
    from dsl41.period import check_manifest_against_segment, read_period_manifest
    from dsl41.runner_clock import EngineError
    from dsl41.runner_history import RunHistoryError, prove_opening

    if opening.get("opens_from_seal") is None:
        return None  # period 1: opened from a catalog and nothing else
    period_id = int(opening["period_id"])
    try:
        prove_opening(root, opening)
        manifest = read_period_manifest(root, period_id)
        if manifest is None:
            raise EngineError(
                f"{root}: periods/{period_id:06d}/manifest.json is not there -- the"
                " opening seal is folded against this period's committed manifest and"
                " the boundary's own artifacts may never be pruned (period-model"
                " ss11, ss12)"
            )
        # PR-22: the manifest and the segment are ONE object written twice
        check_manifest_against_segment(manifest, opening)
        return carried_from_opening(root, opening, manifest)
    except (OSError, EngineError, RunHistoryError) as exc:
        raise typer.Exit(refuse(exc, prefix=where)) from exc


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
        ...,
        help="One or more run roots (dsl41 run --run-root TARGET) -- or the lineage"
        " ANCHOR directory ALONE, which reads every root the registry names, in"
        " period order.",
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
    line -- never silently, and never refused.

    **Pointed at the ESTATE it needs no list at all.** Name the lineage
    anchor directory and the roots come from ss1.3's archive registry, in
    period order, so a lineage that has rolled reads as one table and
    period 1's root is found rather than remembered (PR-02f). A root the
    registry names and the disk does not refuses by name."""
    from dsl41.boundary import is_anchor_dir
    from dsl41.runner_history import RunHistoryError, archived_coverage, read_run_roots

    anchors = [path for path in run_roots if is_anchor_dir(path)]
    if anchors and len(run_roots) > 1:
        raise typer.Exit(
            refuse(
                f"{anchors[0]} is a lineage anchor: name it ALONE. It already names every"
                " root of the estate, and mixing it with roots would fold some of them"
                " twice (period-model ss1.3)"
            )
        )
    roots = list(walk_estate_or_exit_2(anchors[0]).roots()) if anchors else list(run_roots)
    since_at = None if since is None else _naive_utc_arg(since, "--since")
    try:
        rows = read_run_roots(roots, job=job, since=since_at)
        missing = archived_coverage(roots)
    except RunHistoryError as exc:
        raise typer.Exit(refuse(exc)) from exc

    for line in missing:
        # NAMED, never dropped: an archived period contributes no rows, and
        # a table that was quietly shorter for it would be exactly the
        # silent loss this project refuses everywhere else (DL-144)
        typer.echo(f"warning: {line}", err=True)

    if any(row.fidelity == "records_only" for row in rows):
        # Loud on stderr as well as on the row, because the one degraded
        # field that MISLEADS rather than omits is `status`.
        typer.echo(
            "warning: some run roots have no stored inputs, so their rows are"
            " fidelity=records_only: no box rows, no box_name/started_by/job_hash,"
            " and a run closed by KILLJOB or term_run_time reads as RUNNING",
            err=True,
        )

    if any(row.undecided for row in rows):
        # Same posture as the fidelity warning above (DL-156): the flag
        # rides on every row, and the stderr sentence is emitted for every
        # format, exactly as the fidelity warning is.
        typer.echo(
            "warning: some rows are undecided: their newest completion was admitted"
            " and its decision record was never written (the crash window), so the"
            " records do not decide it -- each such row's status stands on what the"
            " records do decide; a full-fidelity read (stored inputs present)"
            " replays the gate and decides them",
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

        def cell(value: object) -> object:
            # csv and json agree on spelling: `--format json` prints
            # true/false, and csv.writer would print Python True/False --
            # `undecided` is the first bool column, so it sets the precedent
            if value is None:
                return ""
            if isinstance(value, bool):
                return "true" if value else "false"
            return value

        buf = io.StringIO()
        writer = csv_mod.writer(buf)
        writer.writerow(fields)
        for row in rows:
            dump = row.model_dump(mode="json")
            writer.writerow([cell(dump[f]) for f in fields])
        typer.echo(buf.getvalue(), nl=False)
    else:
        for line in _runs_table(rows):
            typer.echo(line)
    raise typer.Exit(0)
