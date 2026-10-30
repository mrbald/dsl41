"""The runner verbs (DL-137): execute an estate, rehearse one, read one back.

`run` and `rehearse` drive the engine through the same `start_run`/`resume_run` path: `run` on
the wall clock with real processes, `rehearse` on the virtual clock with scripted adapters.
`journal` and `runs` are their offline readers: a run root's store replayed through a fresh
oracle, and run history folded out of one or more run roots.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import typer

from dsl41.ast_jil import JilFile, render_statement
from dsl41.cli_common import (
    ENVIRONMENT_OPT,
    ESTATE_FILES,
    MANIFEST_OPT,
    PERMIT_UNKNOWN,
    PROPERTIES,
    SEMANTICS_OPT,
    TIMEZONE_MAP_OPT,
    TIMEZONE_OPT,
    check_base_tz,
    estate_args,
    import_tui_or_exit_2,
    load_catalog_and_ast_or_exit_2,
    load_catalog_or_exit_2,
    load_semantics,
    load_tz_aliases,
    refuse,
    statechart_report,
)
from dsl41.ir import CatalogIR
from dsl41.state_machine import EXIT_STATECHART_ERROR, STATECHART_LOG_PREFIX, StatechartError

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable, Iterable
    from datetime import datetime

    from dsl41.oracle_state import Event, TraceEntry
    from dsl41.rehearse_check import CadencePolicy
    from dsl41.runner_access import AccessControl
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_control import ControlServer
    from dsl41.runner_clock import VirtualClock, ZeroDelayCycleError
    from dsl41.runner_scheduler import Scheduler
    from dsl41.runner_config import RuntimeProfile
    from dsl41.runner import Engine
    from dsl41.runner_history import RunRow
    from dsl41.runner_admission import DecisionIndex
    from dsl41.runner_preflight import PreflightItem
    from dsl41.semantics import SemanticSwitches


# ------------------------------------------------------------------- runner
#
# Exit codes for the runner verbs: 0 clean (run: operator-stopped; rehearse: quiescent), 1 the
# engine/estate failed while running (EngineError, oracle refusal mid-run), 2 the run never
# started (preflight ERROR, resume gate, unreadable scenario, unreachable socket). The control-plane
# verbs (cli_control.py) read the same 0/1/2 and split 2 three ways. Exit 3 is per-verb (DL-184):
# rehearse's 3 is --check-cadence finding deviations after a clean play; run's 3 is retired. 4 is
# the control verbs' unknown outcome. 5 is reserved (DL-353).
# run's 6 is a time-half stop (concurrency-model ss4): an engine-made input's time half raised, and
# the engine stopped before it was logged. run's and rehearse's 7 is a statechart error
# (docs/statechart-engine.md ss5): a declared machine hit an assertion failure, and the engine
# stopped before any durable write of it. EXIT_STATECHART_ERROR lives in state_machine.py; a
# restart would run the same build into the same bug.

#: The one stderr line `run --on-transition-violation` prints, whatever its value.
INERT_VIOLATION_OPTION = (
    "dsl41 run: --on-transition-violation is inert and is removed at"
    " the next major release; remove it from the command line"
)
#: `run`'s exit code for a time-half stop (`TimeHalfStop`). The units' `RestartPreventExitStatus`
#: lists it: a restart would meet the same timers (deployment-runbook ss3).
EXIT_TIME_HALF_STOP = 6


def _preflight_or_exit(
    catalog: CatalogIR,
    *,
    execution: bool,
    machine_policy: str = "strict",
    as_machine: "list[str] | None" = None,
    start: "datetime | None" = None,
    default_tz: "str | None" = None,
    tz_aliases: "dict[str, str] | None" = None,
    semantics: "SemanticSwitches | None" = None,
    warns_to_stderr: bool = False,
) -> "list[PreflightItem]":
    """Print ss8 findings; exit 2 on any ERROR; return the WARNs for the caller to journal.

    `start` anchors the DL-56 calendar probes: run passes wall-now, rehearse its virtual --start,
    and an omitted one means now (DL-213). `default_tz` is the --timezone base zone the probes
    fall back to (DL-212). `warns_to_stderr` moves WARN lines off stdout, which rehearse
    --format json owns as one parseable document (DL-180)."""
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
        default_tz=default_tz,
        tz_aliases=tz_aliases,
        semantics=semantics,
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
    """job -> preserve-rendered block for the ss10 `spec` verb: every statement whose subject is
    a catalog job, in file order (insert_job plus any later update_job/delete_job)."""
    texts: dict[str, str] = {}
    for jf in parsed:
        for stmt in jf.statements:
            if stmt.subject in catalog.jobs:
                block = render_statement(stmt)
                texts[stmt.subject] = texts.get(stmt.subject, "") + block
    return texts


class OnViolation(str, Enum):
    """The values `run --on-transition-violation` accepts. The option is inert (DL-353)."""

    refuse = "refuse"
    continue_ = "continue"
    stop = "stop"


def run(
    ctx: typer.Context,
    files: list[Path] = ESTATE_FILES,
    run_root: Path = typer.Option(
        ...,
        "--run-root",
        help="Directory for this run: the journal, runs/, logs/ and control.sock.",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Accepted and ignored: a run root that holds engine.db always continues the"
        " run it records, and one that does not starts a new one.",
    ),  # ss7
    ui: bool = typer.Option(
        False, "--ui", help="Open the terminal UI in this terminal. Quitting it stops the run."
    ),  # ss11
    machine_policy: str = typer.Option(
        "strict",
        "--machine-policy",
        help="What to do with a job whose machine pool spans this host and"
        " others. strict refuses it. local-eligible runs it here with a"
        " warning. A job pinned to another host is always refused.",
    ),  # DL-49
    as_machine: list[str] = typer.Option(
        [],
        "--as-machine",
        help="Machine names this runner answers to. A job whose machine is"
        " one of these, directly or through insert_machine, runs here; any"
        " other machine is refused. Repeatable. Without it the runner uses"
        " its own hostname.",
        # DL-52
    ),
    timezone: str | None = TIMEZONE_OPT,
    timezone_map: Path | None = TIMEZONE_MAP_OPT,
    semantics: list[str] = SEMANTICS_OPT,
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
    access_map: Path = typer.Option(
        None,
        "--access-map",
        help="TOML file mapping principals to access tiers on the control"
        " socket (docs/access-model.md). A missing or invalid file refuses"
        " startup. Without it the socket is owner-only (mode 0600). SIGHUP"
        " reloads the file.",
    ),
    on_violation: OnViolation | None = typer.Option(
        None,
        "--on-transition-violation",
        metavar="POLICY",
        help="Inert; removed at the next major release. Every"
        " state machine is a declared statechart, and none reports a broken"
        " transition, so the option changes nothing. Given, it prints a"
        " deprecation line.",
    ),
    manifest: Path | None = MANIFEST_OPT,
    environment: str | None = ENVIRONMENT_OPT,
    grace_term: float | None = typer.Option(
        None,
        "--grace-term",
        help="Seconds from TERM to KILL when a job is stopped, for this run only."
        " Default: grace_term_s in --run-root's engine.toml.",
    ),
    grace_kill: float | None = typer.Option(
        None,
        "--grace-kill",
        help="Seconds from KILL to the job's exit record, for this run only."
        " Default: grace_kill_s in engine.toml.",
    ),
    snapshot_interval: float | None = typer.Option(
        None,
        "--snapshot-interval",
        help="Seconds between snapshots of the engine's state, for this run only."
        " Default: snapshot_interval_s in engine.toml.",
    ),
    retention_days: int | None = typer.Option(
        None,
        "--retention-days",
        help="Days of journal kept behind the latest snapshot, for this run only."
        " Default: retention_days in engine.toml.",
    ),
) -> None:
    """Run an estate on this machine with real processes and a wall clock.

    FILES form the estate, or --manifest names the estate manifest that
    declares them with their layers, timezone, map and switches.

    The engine schedules jobs by calendar, starts real processes,
    records every input in --run-root's engine.db, and listens on a
    control socket under --run-root. SIGINT, SIGTERM or 'dsl41 engine
    stop' drains it: every running job is asked to stop (TERM, then KILL
    after the grace), the engine records how each ended and exits 0.
    Jobs are tethered: when the engine dies, its jobs are terminated
    and their wrappers record it. The first run writes engine.toml with
    the default stop grace, snapshot cadence and retention; the
    --grace-term, --grace-kill, --snapshot-interval and --retention-days
    flags override it for one run.

    While it runs, use 'dsl41 query' and 'dsl41 sendevent' from another
    terminal, or open the terminal UI with --ui here or 'dsl41 ui'
    elsewhere.

    Exit codes: 0 stopped by the operator (a drain, clean or not); 1 the
    engine or the estate failed while running, a failed shutdown record
    included; 2
    the run never started (a preflight error, a refused resume, an
    unreadable input); 6 a time-half stop (the time half of an
    engine-made input raised, and the engine stopped before logging it);
    7 a statechart error (a declared machine hit an assertion failure).
    Exits 3 and 5 are retired and unused.
    """
    # Design: runner-design ss1, ss9, ss10, ss6a
    import asyncio

    from datetime import UTC, datetime

    files, properties, timezone, timezone_map, semantics = estate_args(
        manifest, environment, files, properties, timezone, timezone_map, semantics
    )
    if on_violation is not None:
        typer.echo(INERT_VIOLATION_OPTION, err=True)
    if ui:
        import_tui_or_exit_2()  # fail before the engine starts, not after
    catalog, parsed, fingerprint = load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)
    tz_aliases = load_tz_aliases(timezone_map)
    check_base_tz(timezone, tz_aliases)
    overrides = load_semantics(semantics)
    from dsl41.semantics import resolve as resolve_switches

    warns = _preflight_or_exit(
        catalog,
        execution=True,
        machine_policy=machine_policy,
        as_machine=as_machine,
        start=datetime.now(UTC).replace(tzinfo=None),
        default_tz=timezone,
        tz_aliases=tz_aliases,
        semantics=resolve_switches(overrides),
    )
    from dsl41.idempotence import IdempotenceError, load_exceptions
    from dsl41.runner_clock import EngineError
    from dsl41.runner_config import runtime_profile_from_cli

    try:
        marked = {} if manifest is None else dict(load_exceptions(manifest.parent).reasons)
    except IdempotenceError as exc:
        raise typer.Exit(refuse(exc)) from exc
    # ss2.1: the launch options that change interpretation or dispatch, as one typed value;
    # `runtime_hash` tells an engine launched --timezone UTC from one launched Europe/Zurich
    profile = runtime_profile_from_cli(
        timezone=timezone,
        tz_aliases=tz_aliases,
        as_machine=as_machine,
        machine_policy=machine_policy,
        semantics=overrides,
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
                    access_map=access_map,
                    non_idempotent=marked,
                    overrides={
                        "grace_term_s": grace_term,
                        "grace_kill_s": grace_kill,
                        "snapshot_interval_s": snapshot_interval,
                        "retention_days": retention_days,
                    },
                )
            )
        )
    except EngineError as exc:
        # start/resume gates (existing journal, hash/domain mismatch, live socket): never started
        raise typer.Exit(refuse(exc)) from exc


def _attach_tui(server_path: Path, owns_run: bool) -> tuple[Any, "asyncio.Task[Any]"]:
    """Construct the `--ui` TUI and start it as a task in this loop, a client of the control
    socket ONLY (ss11). The import stays local so `--ui` is the only path that needs textual."""
    import asyncio

    from dsl41.runner_tui import RunnerApp

    tui = RunnerApp(server_path, owns_run=owns_run)
    return tui, asyncio.ensure_future(tui.run_async())


def _tui_failure(
    tui: Any, ui_task: "asyncio.Task[Any] | None", done: "set[asyncio.Task[Any]]"
) -> str | None:
    """F3 (DL-46 item 10): a raised exception is a crash; so is a nonzero `return_code`, which is
    textual's fatal-error path returning from `run_async` (`App.exit()` defaults to 0, so a
    nonzero value is never an operator stop). None when the TUI task never finished or ended
    clean."""
    if ui_task is None or ui_task not in done or ui_task.cancelled():
        return None
    tui_exc = ui_task.exception()
    if tui_exc is not None:
        return repr(tui_exc)
    if tui is not None and tui.return_code:
        return f"exit code {tui.return_code}"
    return None


def _preflight_launch(access_map: "Path | None") -> None:
    """`run`'s read-only check before anything is created: the access policy's load. A failing
    check raises the `AccessError` it met."""
    if access_map is not None:
        # the access-model ss4 refusal must write NOTHING: the loader arming runs later is called
        # here read-only, before the root is claimed. `socket_group` resolution is the loader's own
        # (DL-152), so an unknown group refuses here in the loader's words
        from dsl41.runner_access import load_policy

        load_policy(access_map, generation=1)


def _install_signal_handlers(stop: "Callable[[], object]", access: "AccessControl | None") -> None:
    """Route SIGINT and SIGTERM to `stop`, the engine's `stop_requested` (a drain; a second one
    moves its deadline to now; in STARTING, `shutdown_during_startup`), and SIGHUP to the access
    reload."""
    import asyncio
    import signal as signal_mod

    loop = asyncio.get_running_loop()
    for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except (NotImplementedError, ValueError, RuntimeError):
            # non-main-thread embedding (test harnesses): stoppable only by engine failure
            pass
    if access is not None:
        # access-model ss7: explicit reload; a failed one keeps the old policy and writes the
        # receipt, never kills the engine
        try:
            loop.add_signal_handler(signal_mod.SIGHUP, access.reload)
        except (NotImplementedError, ValueError, RuntimeError):
            pass


class _EarlyStops:
    """The stops that land before the engine exists, kept for it: the signal's callback may run
    after the engine is built, so a stop it brings then goes straight to the engine."""

    def __init__(self) -> None:
        self.kept = 0
        self.engine: "Engine | None" = None

    def stop(self) -> None:
        if self.engine is not None:
            self.engine.request_stop()
        else:
            self.kept += 1

    def hand_to(self, engine: "Engine") -> None:
        self.engine = engine
        for _ in range(self.kept):
            engine.request_stop()
        self.kept = 0


class _RunEnd(NamedTuple):
    """What `_await_run_end` hands `_stop_and_judge`: the loop task, the attached TUI and its
    task, and the tasks that finished."""

    loop_task: "asyncio.Task[Any]"
    tui: Any
    ui_task: "asyncio.Task[Any] | None"
    done: "set[asyncio.Task[Any]]"


async def _await_started(engine: "Engine", loop_task: "asyncio.Task[Any]") -> bool:
    """Wait for the engine loop to finish a restart's recovery and record `engine started`
    (startup step 8), or for the loop to end first: a crash, or a stop request during the
    recovery, which records `shutdown_during_startup` and exits 0. True when it started."""
    import asyncio

    started = asyncio.ensure_future(engine.started.wait())
    await asyncio.wait({loop_task, started}, return_when=asyncio.FIRST_COMPLETED)
    started.cancel()
    return engine.started.is_set() and not loop_task.done()


async def _await_run_end(
    engine: "Engine",
    server: "ControlServer",
    loop_task: "asyncio.Task[Any]",
    ui: bool,
) -> _RunEnd:
    """Wait for the first thing that ends the run: the loop (a drain that reached STOPPED, or a
    crash), the server's failure or the attached TUI. Quitting the attached TUI drains the
    engine, as a stop signal does, and the run ends with the loop."""
    import asyncio

    failed_task = asyncio.ensure_future(server.failed.wait())
    ui_task: asyncio.Task[Any] | None = None
    tui = None
    if ui:
        tui, ui_task = _attach_tui(server.path, owns_run=True)
    waiters = {loop_task, failed_task}
    waiters |= {ui_task} if ui_task is not None else set()
    done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    if ui_task is not None and ui_task in done and not loop_task.done():
        engine.request_stop()
        await asyncio.wait({loop_task, failed_task}, return_when=asyncio.FIRST_COMPLETED)
        done |= {task for task in (loop_task, failed_task) if task.done()}
    failed_task.cancel()
    return _RunEnd(loop_task, tui, ui_task, done)


async def _stop_and_judge(
    engine: "Engine",
    server: "ControlServer",
    end: _RunEnd,
) -> int:
    """How the run ended: tear down the TUI, the server and the engine; return the exit code."""
    import asyncio
    import contextlib

    from dsl41.runner_admission import TimeHalfStop

    loop_task, tui, ui_task, done = end
    code = 0
    try:
        tui_failure = _tui_failure(tui, ui_task, done)
        if tui is not None and ui_task is not None and ui_task not in done:
            tui.exit()  # engine crash or signal: detach the viewer first
            with contextlib.suppress(Exception):
                await ui_task
        # how the loop ended, if on its own; a loop the server cancelled ended for the server's
        # error, read below
        ended = loop_task in done and not loop_task.cancelled()
        outcome = loop_task.exception() if ended else None
        if isinstance(outcome, TimeHalfStop):
            # not a crash: a restart meets the same timers, so its own code keeps the unit down
            # until a fixed build runs (concurrency-model ss4)
            typer.echo(f"engine stopped: {outcome}", err=True)
            code = EXIT_TIME_HALF_STOP
        elif isinstance(outcome, StatechartError):
            # an assertion failure, not a crash: every report line goes out under one prefix, and
            # its own code keeps the unit down until a fixed build runs
            # (docs/statechart-engine.md ss5)
            code = statechart_report(outcome)
        elif server.statechart_error is not None:
            # the same report and exit for one a control handler met; the handler sent no reply,
            # hung up and cancelled the loop
            code = statechart_report(server.statechart_error)
            # never a raw await: a loop that crashed in the same instant must not skip the
            # teardown or the report's precedence
            loop_task.cancel()
            await asyncio.wait({loop_task})
            if not loop_task.cancelled() and loop_task.exception() is not None:
                typer.echo(f"engine failed: {loop_task.exception()}", err=True)
        elif ended and outcome is None and engine.exit_code is not None:
            # the drain reached STOPPED: a signal, `engine stop`, or quitting the attached TUI
            # (ss11: --ui tethers the run to this terminal; `dsl41 ui` attaches viewers that
            # must not stop it). Its record says whether every attempt settled (spec-drain)
            code = engine.exit_code
            if tui_failure is not None:
                typer.echo(f"TUI failed: {tui_failure}", err=True)
                code = 1
            leaving = engine.leaving_record
            typer.echo(f"engine stopped: {leaving}" if leaving else "engine stopped")
        else:  # hold_open never quiesces: this is a crash
            typer.echo(f"engine failed: {outcome}", err=True)
            code = 1
        if not loop_task.done():
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
    finally:
        # whatever the branch above met, the server and the engine are torn down
        try:
            await server.close()
        finally:
            await engine.shutdown()
    return code


def _finish_run(
    server: "ControlServer",
    code: int,
) -> int:
    """After the teardown: judge the server's statechart error against the exit code. Returns the
    exit code."""
    if server.statechart_error is not None and code != EXIT_STATECHART_ERROR:
        # recorded while a loop outcome took precedence, or met while the server closed. A crash or a clean stop becomes 7; a halt the engine chose
        # keeps its code
        statechart_report(server.statechart_error)
        if code in (0, 1):
            code = EXIT_STATECHART_ERROR
        else:
            typer.echo(
                f"{STATECHART_LOG_PREFIX}: the engine had already stopped with exit {code}",
                err=True,
            )
    return code


async def _serve_run(
    catalog: CatalogIR,
    run_root: Path,
    resume: bool,
    warns: "list[PreflightItem]",
    *,
    profile: "RuntimeProfile",
    ui: bool = False,
    spec_texts: "dict[str, str] | None" = None,
    estate_fingerprint: "dict[str, str] | None" = None,
    access_map: "Path | None" = None,
    non_idempotent: "dict[str, str] | None" = None,
    overrides: "dict[str, float | int | None] | None" = None,
) -> int:
    """`dsl41 run`, from the acquire to the last teardown.

    A run root that holds `engine.db` continues the run it records, whatever `resume` says, so
    a supervisor that reruns the same command line restarts the engine; one that does not
    starts a new run, and a missing root is created.

    The profile is REQUIRED and is the one source of every launch option read here (timezone,
    alias table, the FW window) (DL-137); `engine.toml` with `overrides` over it is the source
    of the stop grace, the snapshot cadence and the retention."""
    import asyncio

    from datetime import UTC, datetime

    from dsl41.runner_startup import start_run, wire_from_profile
    from dsl41.runner_control import ControlServer
    from dsl41.runner_startup import resume_run as _resume_run
    from dsl41.runner_access import AccessError
    from dsl41.runner_clock import EngineError, RealClock
    from dsl41.runner_config import load_settings

    from dsl41.runner_ledger import acquire_run_root

    clock = RealClock()
    try:
        _preflight_launch(access_map)
    except (EngineError, AccessError) as exc:
        return refuse(exc)
    # ACQUIRE first (S6a): everything after it reads, replays or launches in a root this process
    # must lead
    try:
        lock = acquire_run_root(run_root)
    except EngineError as exc:
        return refuse(exc)
    # ONE teardown, for every way out (DL-145). The leader lock is taken here and given back
    # here, after the store; `release` is idempotent, so the S6a close inside `journal.close`
    # stays where the record ends.
    from dsl41.runner_journal import STORE_NAME

    resume = (run_root / STORE_NAME).exists()
    # every SIGTERM is the lifecycle's `stop_requested`: one that lands while the engine is
    # built is kept and handed to it as soon as it exists (spec-drain)
    early = _EarlyStops()
    _install_signal_handlers(early.stop, None)
    engine: "Engine | None" = None
    code = 1
    running = False
    try:
        try:
            # read, not written: a root the restart refuses gains no file
            settings = load_settings(run_root, create=False).overridden(**(overrides or {}))
        except (EngineError, ValueError) as exc:
            return refuse(exc)
        # ONE wiring builder (DL-137): genesis and restart build adapters and scheduler from the
        # PROFILE and the settings
        try:
            wiring = wire_from_profile(
                catalog, profile, start=datetime.now(UTC).replace(tzinfo=None), settings=settings
            )
        except EngineError as exc:
            return refuse(exc)
        try:
            if resume:
                engine = await _resume_run(
                    catalog,
                    run_root,
                    clock=clock,
                    adapters=wiring.adapters,
                    scheduler=wiring.scheduler,
                    hold_open=True,
                    lock=lock,
                    # the launch options no wired object can report (machine identity and policy):
                    # without them the core gate inherits them from the store and can never
                    # disagree with it (DL-151)
                    declared=profile,
                    machines=wiring.machines,
                    settings=settings,
                )
            else:
                engine = start_run(
                    catalog,
                    run_root,
                    clock=clock,
                    adapters=wiring.adapters,
                    scheduler=wiring.scheduler,
                    hold_open=True,
                    lock=lock,
                    launched=profile,
                    non_idempotent=non_idempotent,
                    machines=wiring.machines,
                    settings=settings,
                )
        except StatechartError as exc:
            # a replayed input that meets one: the report names the input, and exit 7 keeps
            # the unit down (docs/statechart-engine.md ss5)
            return statechart_report(exc)
        except EngineError as exc:
            return refuse(exc)
        # the first run writes the defaults out (spec-wiring 1b); the read above already
        # refused a file that does not read
        load_settings(run_root)
        # what the restart did not apply: E9's missed scheduler ticks (DL-91)
        for ev, reason in engine.drops:
            typer.echo(
                f"dropped {ev.kind} {ev.job() or ''} @ {ev.at.isoformat()}: {reason}", err=True
            )
        if warns and engine.journal is not None:
            engine.journal.preflight(warns, clock.now())
        access = None
        if access_map is not None:
            # access-model ss4: a configured path that does not load REFUSES;
            # it never falls back to owner-wide authority
            from dsl41.runner_access import AccessControl, AccessError

            try:
                access = AccessControl.arm(access_map, run_root)
            except AccessError as exc:
                await engine.shutdown()
                return refuse(exc)
        server = ControlServer(
            engine,
            run_root / "control.sock",
            spec_texts=spec_texts,
            estate_fingerprint=estate_fingerprint,
            access=access,
        )
        # Handlers go in BEFORE the socket is bound, so a SIGHUP after this engine bound
        # control.sock is a reload, not a kill. A stop that came while the engine was built
        # (the replay, step 5) is the lifecycle's now: STARTING + stop_requested
        early.hand_to(engine)
        _install_signal_handlers(engine.request_stop, access)
        # the loop carries out the restart's steps 6 and 7 and records step 8; the socket is
        # bound only then (spec-wiring 1b)
        loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        # a control handler's StatechartError stops the run as one from the loop does
        # (statechart-engine ss5): the handler cancels the loop at once and the run wakes on
        # `failed`
        server.on_statechart_error = loop_task.cancel
        if not await _await_started(engine, loop_task):
            done = {loop_task} if loop_task.done() else set()
            code = await _stop_and_judge(engine, server, _RunEnd(loop_task, None, None, done))
            return code
        try:
            await server.start()
        except EngineError as exc:
            # the handlers stay until `loop.close()`, as on the success path; the tasks the
            # restart started stop before the store closes and the lock is released below
            loop_task.cancel()
            await asyncio.wait({loop_task})
            await engine.shutdown()
            return refuse(exc)
        running = True
        typer.echo(f"engine up; control socket: {server.path}")
        end = await _await_run_end(engine, server, loop_task, ui)
        code = await _stop_and_judge(engine, server, end)
        # the CLI wrapper reports a statechart error as exit 7
        code = _finish_run(server, code)
        return code
    finally:
        # EACH step guaranteed, whatever the one before it did (DL-145): the shutdown record,
        # the store's close and the leader lock are obligations to different things. A failing
        # close must PROPAGATE, but must not also strand the lock, which would refuse the
        # operator's retry with "held by another engine".
        try:
            if engine is not None and engine.journal is not None:
                try:
                    _record_shutdown(engine, running=running)
                finally:
                    engine.journal.close()
        finally:
            lock.release()


def _record_shutdown(engine: "Engine", *, running: bool) -> None:
    """The engine's last record when the lifecycle wrote none. Only the lifecycle's STOPPED
    writes `clean_shutdown` or `unclean_shutdown` (spec-drain); an engine that never reached
    RUNNING records `shutdown_during_startup` and no snapshot; one that failed while running
    records nothing, so the restart's fold sees where it stopped (a drain it interrupted
    included)."""
    assert engine.journal is not None
    if engine.shutdown_recorded or running:
        return
    try:
        engine.journal.engine("shutdown_during_startup", engine.clock.now())
    except Exception as exc:  # noqa: BLE001 -- the record is best effort; the close follows
        typer.echo(f"dsl41: the shutdown record was not written: {exc}", err=True)


class RehearseFormat(str, Enum):
    """`rehearse --format`: one axis for what would otherwise be two flags (the DL-75 shape)."""

    text = "text"  # trace lines
    summary = "summary"  # trace lines + the per-job rollup
    json = "json"  # one document: trace + rollup


class SweepKind(str, Enum):
    """`rehearse --check-cadence --sweep` (DL-184)."""

    fail = "fail"  # one replay per producer, first run scripted to FAILURE
    flags = "flags"  # one replay per (global, region value, reset variant)


def _trace_line(entry: "TraceEntry") -> str:
    """The one spelling of a printed trace line, shared by rehearse and the journal replay."""
    return f"{entry.at.isoformat()} {entry.job} {entry.transition} [{entry.cause}]"


def _scenario_adapter(scenario: Path | None) -> "tuple[FakeAdapter, list[Event]]":
    """The rehearse scenario file, parsed into the scripted adapter and the events to inject.

    The docstring on `rehearse` states the accepted shapes. Raises
    OSError/ValueError/TypeError/KeyError; the caller owns the refusal."""
    import json as json_mod

    from dsl41.canon import naive_utc
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
    # An event's `at` reads as --start does (_naive_utc_arg): the engine sees naive UTC only
    # (DL-44 (5)); canon.naive_utc owns the conversion (DL-178 (j), DL-179 (e)).
    events = [
        (ev := Event.model_validate(entry)).model_copy(update={"at": naive_utc(ev.at)})
        for entry in data.get("events", [])
    ]
    return FakeAdapter(script, default=default, park=park), events


def _rollup(trace: "list[TraceEntry]", catalog_jobs: Iterable[str]) -> dict[str, dict[str, Any]]:
    """The per-job rollup rows: a run is a transition INTO STARTING and the final status is the
    last transition's target (out-of-band markers carry no "->" and count for neither).
    Never-started catalog jobs appear with runs=0. The count stays STARTING-based (DL-184); the
    cadence check reads run_number deltas from the store."""
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
    return {
        name: {"runs": run_counts.get(name, 0), "final_status": final_status.get(name)}
        for name in names
    }


def _rehearsal_doc(trace: "list[TraceEntry]", catalog_jobs: Iterable[str]) -> dict[str, Any]:
    """The --format json document: the full trace plus the rollup rows, built only on the json
    path."""
    return {
        "trace": [t.model_dump(mode="json") for t in trace],
        "jobs": _rollup(trace, catalog_jobs),
    }


def _emit_rehearsal(
    trace: list[TraceEntry], catalog_jobs: Iterable[str], *, fmt: RehearseFormat
) -> None:
    """The rehearse report: the trace, and the per-job rollup ("which job fired how often",
    DL-180)."""
    import json as json_mod

    if fmt is RehearseFormat.json:
        typer.echo(json_mod.dumps(_rehearsal_doc(trace, catalog_jobs), sort_keys=True))
        return
    for t in trace:
        typer.echo(_trace_line(t))
    if fmt is RehearseFormat.summary:
        typer.echo("-- summary: runs per job --")
        for name, row in _rollup(trace, catalog_jobs).items():
            typer.echo(f"{name} runs={row['runs']} final={row['final_status'] or '-'}")


def _emit_cadence_check(
    engine: "Engine",
    catalog: "CatalogIR",
    runs_before: dict[str, int],
    *,
    fmt: RehearseFormat,
    start_dt: "datetime",
    horizon: "datetime",
    timezone: str | None,
    tz_aliases: dict[str, str] | None,
    policy: "CadencePolicy | None",
    injected_start: dict[str, int],
    injected_force: dict[str, int],
    parked_fw: frozenset[str],
    no_success: frozenset[str],
    cycle: "ZeroDelayCycleError | None",
    sweeps: "list[SweepKind]",
    adapter: "FakeAdapter",
    events: "list[Event]",
) -> None:
    """The --check-cadence report and exit (DL-184): expected bounds from a fresh Scheduler plus
    the wake-source walk, observed counts as run_number deltas, deviations exit 3. text = table;
    summary = trace + table; json = the document plus one nested cadence_check block. --sweep
    fail replays per producer through play_once; case progress goes to stderr."""
    import json as json_mod

    from dsl41.derive import derive_graph
    from dsl41.rehearse_check import (
        Interpretation,
        LostInput,
        SweepRefused,
        compare,
        expected_bounds,
        fail_sweep_producers,
        lost_lines,
        render_text,
        run_fail_sweep,
        run_flag_sweep,
        scheduled_ticks,
    )

    observed = {
        name: engine.oracle.store.runtime(name).run_number - runs_before[name]
        for name in catalog.jobs
    }
    graph = derive_graph(catalog)
    reading = Interpretation(
        default_tz=timezone, tz_aliases=tz_aliases, semantics=engine.oracle.semantics
    )
    ticks = scheduled_ticks(catalog, start=start_dt, horizon=horizon, reading=reading)
    bounds = expected_bounds(
        catalog,
        graph,
        ticks,
        injected_start=injected_start,
        injected_force=injected_force,
        policy=policy,
        parked=parked_fw,
        no_success_exit=no_success,
    )
    check = compare(catalog, bounds, observed, start=start_dt, horizon=horizon, cycle=cycle)
    if sweeps and cycle is not None:
        # an aborted happy play leaves truncated, spin-inflated counts, a baseline of fiction
        # that would burn the whole instant budget per case. The cycle finding already exits 3;
        # the sweeps list omits what did not run.
        typer.echo("sweeps skipped: the happy path tripped the zero-delay guard", err=True)
    elif sweeps:
        progress = lambda line: typer.echo(line, err=True)  # noqa: E731
        # every sweep plays to its end; the inputs they lost are reported together
        lost: list[LostInput] = []
        if SweepKind.fail in sweeps:
            try:
                cases, sweep_findings = run_fail_sweep(
                    catalog,
                    observed,
                    bounds,
                    adapter,
                    events,
                    start=start_dt,
                    horizon=horizon,
                    reading=reading,
                    producers=fail_sweep_producers(catalog, graph),
                    parked=parked_fw,
                    progress=progress,
                )
            except SweepRefused as exc:
                lost.extend(exc.lost)
            else:
                check.sweeps.append(SweepKind.fail.value)
                check.fail_sweep = cases
                check.findings.extend(sweep_findings)
        if SweepKind.flags in sweeps:
            try:
                fcases, flag_findings, uncovered = run_flag_sweep(
                    catalog,
                    graph,
                    ticks,
                    adapter,
                    events,
                    start=start_dt,
                    horizon=horizon,
                    reading=reading,
                    injected_start=injected_start,
                    injected_force=injected_force,
                    policy=policy,
                    parked=parked_fw,
                    no_success_exit=no_success,
                    progress=progress,
                )
            except SweepRefused as exc:
                lost.extend(exc.lost)
            else:
                check.sweeps.append(SweepKind.flags.value)
                check.flag_sweep = fcases
                check.flag_uncovered = uncovered
                check.findings.extend(flag_findings)
        if lost:
            for line in lost_lines(lost):
                typer.echo(line, err=True)
            raise typer.Exit(1)
    trace = engine.oracle.trace()
    if fmt is RehearseFormat.json:
        doc = _rehearsal_doc(trace, catalog.jobs)
        doc["cadence_check"] = check.model_dump(mode="json")
        typer.echo(json_mod.dumps(doc, sort_keys=True))
    else:
        if fmt is RehearseFormat.summary:
            for t in trace:
                typer.echo(_trace_line(t))
        for line in render_text(check):
            typer.echo(line)
    if check.findings:
        raise typer.Exit(3)


def _scenario_completion(raw: object, where: str) -> tuple[float, int]:
    """One scenario completion, in BOTH spellings: the list `[duration_s, exit_code]` and the
    runs-entry object `{"duration_s": S, "exit_code": C}`. Malformed shapes refuse with the two
    accepted forms named."""
    if isinstance(raw, dict):
        if "duration_s" not in raw or "exit_code" not in raw:
            raise ValueError(f"{where}: object form needs duration_s and exit_code")
        return float(raw["duration_s"]), int(raw["exit_code"])
    if isinstance(raw, list) and len(raw) == 2:
        return float(raw[0]), int(raw[1])
    raise ValueError(
        f'{where}: expected [duration_s, exit_code] or {{"duration_s": S, "exit_code": C}}'
    )


def _rehearsal_engine(
    catalog: CatalogIR,
    run_root: "Path | None",
    clock: "VirtualClock",
    adapters: "dict[str, FakeAdapter]",
    scheduler: "Scheduler",
    *,
    timezone: "str | None",
    tz_aliases: "dict[str, str] | None",
    overrides: "dict[str, str]",
) -> "Engine":
    """The engine `rehearse` plays: genesis of `run_root`'s store when one was named, else of a
    `MemoryStore`. An `EngineError` is a refusal."""
    from dsl41.runner_clock import EngineError
    from dsl41.runner_config import runtime_profile_from_cli
    from dsl41.runner_startup import start_run

    # the profile the rehearsal interprets the estate under, its timezone above all, belongs in
    # the configuration row, or the record claims a run it did not make (ss2.1)
    launched = runtime_profile_from_cli(
        timezone=timezone, tz_aliases=tz_aliases, semantics=overrides
    )
    try:
        return start_run(
            catalog,
            run_root,
            clock=clock,
            adapters=adapters,
            scheduler=scheduler,
            launched=launched,
        )
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc


def rehearse(
    files: list[Path] = ESTATE_FILES,
    scenario: Path = typer.Option(
        None,
        "--scenario",
        help="JSON scenario with scripted job outcomes and events to inject."
        " An event's instant may carry a zone and is read as UTC when it has none."
        " The shape is described above.",
    ),
    start: str = typer.Option(
        None, "--start", help="Virtual clock start as an ISO datetime. Defaults to now, UTC."
    ),
    hours: float = typer.Option(
        24.0,
        "--hours",
        help="Horizon. The rehearsal ends when no work remains before start + HOURS.",
    ),
    output: RehearseFormat = typer.Option(
        RehearseFormat.text,
        "--format",
        metavar="FORMAT",
        help="text: trace lines. summary: trace plus per-job run counts and"
        " final statuses. json: one document with both. With"
        " --check-cadence, text is the comparison table, summary is trace"
        " plus table, and json gains a cadence_check block.",
    ),
    check_cadence: bool = typer.Option(
        False,
        "--check-cadence",
        help="Compare each job's run count with its computed bound and"
        " exit 3 on deviations. Latch effects across days need --hours 48"
        " or more.",
    ),  # DL-182
    cadence_policy: Path = typer.Option(
        None,
        "--cadence-policy",
        help="JSON file of declared cadence exceptions. The shape is"
        " described above. Needs --check-cadence.",
    ),
    sweep: list[SweepKind] = typer.Option(
        None,
        "--sweep",
        metavar="SWEEP",
        help="Extra cadence plays, fail or flags, as described above."
        " Repeatable. Needs --check-cadence. Cannot be combined with"
        " --run-root.",
    ),
    timezone: str | None = TIMEZONE_OPT,
    timezone_map: Path | None = TIMEZONE_MAP_OPT,
    semantics: list[str] = SEMANTICS_OPT,
    run_root: Path = typer.Option(
        None, "--run-root", help="Also write the record to engine.db under this directory."
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
    manifest: Path | None = MANIFEST_OPT,
    environment: str | None = ENVIRONMENT_OPT,
) -> None:
    r"""Play an estate under a virtual clock and print what would happen.

    Rehearsal runs the same engine as 'run' with scripted job outcomes,
    so a 24-hour estate plays in seconds. The printed trace shows what
    production would do. FILES form the estate, or --manifest names the
    estate manifest that declares them, as for 'run'.

    Scenario file (JSON, every key optional):
      {"adapter": {"default": \[duration_s, exit_code]
                              | {"duration_s": S, "exit_code": C} | null,
                   "park": \[job, ...],
                   "runs": \[{"job": J, "run_number": N,
                             "duration_s": S, "exit_code": C}
                            | {"job": J, "run_number": N, "park": true}, ...]},
       "events": \[{"at": ISO, "kind": KIND, "payload": {...}}, ...]}
    A null default parks every unscripted run, so the script drives
    completions. "park" parks every run of the named jobs, for example a
    file watcher that must not fire. A runs entry with "park": true parks
    that one run. Precedence: runs entry, then park, then default.

    --check-cadence plays the rehearsal and compares each job's run count
    with a computed bound. Deviations exit 3. Jobs whose bound cannot be
    computed are reported as unchecked. Scenario events are limited to
    STARTJOB, FORCE_STARTJOB and SET_GLOBAL, and unscripted file watchers
    are parked. --cadence-policy declares intended exceptions:
      {"schema_version": 1,
       "policies": {JOB: {"max_runs": N | null, "reason": "..."}}}
    null means unchecked. The reason is printed in the report.

    --sweep fail replays the estate once per upstream job, with that
    job's first run failing. Jobs that then run fewer times are listed
    as suppressed runs. A job that runs more often than its bound still
    deviates and exits 3. Box, parked and retrying producers are reported
    as skipped or inconclusive rather than covered.

    --sweep flags replays the estate once per global, per value its
    conditions test, with and without a reset. Consumers gated by that
    global are checked under the pinned value. Globals past the case
    ceiling stay unchecked and are named in the report.

    Exit codes: 0 the estate went quiet within the horizon; 1 the engine
    or the estate failed while playing; 2 the rehearsal never started
    (an unreadable input or scenario); 3 --check-cadence found
    deviations.
    """
    # Design: runner-design ss9, DL-182, DL-184, SEM-09, DL-180, L022 (dynamic)
    import asyncio

    from datetime import UTC, datetime, timedelta

    from dsl41.oracle_state import OracleError
    from dsl41.runner_clock import EngineError, VirtualClock, ZeroDelayCycleError
    from dsl41.runner_scheduler import Scheduler
    from dsl41.semantics import resolve as resolve_switches

    files, properties, timezone, timezone_map, semantics = estate_args(
        manifest, environment, files, properties, timezone, timezone_map, semantics
    )
    if cadence_policy is not None and not check_cadence:
        raise typer.Exit(refuse("--cadence-policy requires --check-cadence"))
    sweeps = list(sweep or [])
    if sweeps and not check_cadence:
        raise typer.Exit(refuse("--sweep requires --check-cadence"))
    if sweeps and run_root is not None:
        raise typer.Exit(
            refuse(
                "--sweep refuses --run-root: a sweep replays the estate, one journal claims one run"
            )
        )
    catalog, _, _ = load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)
    overrides = load_semantics(semantics)
    start_dt = (
        _naive_utc_arg(start, "--start")
        if start
        else datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    )
    tz_aliases = load_tz_aliases(timezone_map)
    check_base_tz(timezone, tz_aliases)
    switches = resolve_switches(overrides)
    warns = _preflight_or_exit(
        catalog,
        execution=False,
        start=start_dt,
        default_tz=timezone,
        tz_aliases=tz_aliases,
        semantics=switches,
        warns_to_stderr=output is RehearseFormat.json,
    )
    try:
        adapter, events = _scenario_adapter(scenario)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise typer.Exit(refuse(exc, prefix=f"scenario {scenario}")) from exc
    parked_fw: frozenset[str] = frozenset()
    no_success: frozenset[str] = frozenset()
    policy = None
    injected_start: dict[str, int] = {}
    injected_force: dict[str, int] = {}
    if check_cadence:
        from dsl41.rehearse_check import CadenceCheckError, check_adapter, load_policy
        from dsl41.rehearse_check import scenario_budgets

        try:
            if cadence_policy is not None:
                policy = load_policy(cadence_policy, catalog)
            injected_start, injected_force = scenario_budgets(events)
        except CadenceCheckError as exc:
            raise typer.Exit(refuse(exc)) from exc
        adapter, parked_fw, no_success = check_adapter(catalog, adapter)
    clock = VirtualClock(start_dt)
    try:
        scheduler = Scheduler(
            catalog,
            start=start_dt,
            default_tz=timezone,
            tz_aliases=tz_aliases,
            semantics=switches,
        )
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc
    adapters = {"CMD": adapter, "FW": adapter}
    engine = _rehearsal_engine(
        catalog,
        run_root,
        clock,
        adapters,
        scheduler,
        timezone=timezone,
        tz_aliases=tz_aliases,
        overrides=overrides,
    )
    if warns and engine.journal is not None:
        engine.journal.preflight(warns, start_dt)
    runs_before = {name: engine.oracle.store.runtime(name).run_number for name in catalog.jobs}
    for ev in events:
        engine.inject(ev, source="control")
    horizon = start_dt + timedelta(hours=hours)

    async def _play() -> None:
        try:
            await engine.run_until_quiescent(horizon)
        finally:
            await engine.shutdown()

    cycle: ZeroDelayCycleError | None = None
    try:
        asyncio.run(_play())
    except ZeroDelayCycleError as exc:
        if not check_cadence:
            typer.echo(f"rehearse failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        cycle = exc  # the check's own finding, not a shell failure (DL-184)
    except (EngineError, OracleError) as exc:
        typer.echo(f"rehearse failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        if engine.journal is not None:
            engine.journal.close()
    from dsl41.rehearse_check import lost_inputs, lost_lines

    lost = lost_inputs(engine)
    if lost:
        # a scripted event the dry apply refused never reached the trace, and a quarantined
        # engine-made input applied its time half only: no silent loss, so each is named and the
        # play fails (concurrency-model ss4)
        for line in lost_lines(lost):
            typer.echo(line, err=True)
        raise typer.Exit(1)
    if not check_cadence:
        _emit_rehearsal(engine.oracle.trace(), catalog.jobs, fmt=output)
        return
    _emit_cadence_check(
        engine,
        catalog,
        runs_before,
        fmt=output,
        start_dt=start_dt,
        horizon=horizon,
        timezone=timezone,
        tz_aliases=tz_aliases,
        policy=policy,
        injected_start=injected_start,
        injected_force=injected_force,
        parked_fw=parked_fw,
        no_success=no_success,
        cycle=cycle,
        sweeps=sweeps,
        adapter=adapter,
        events=events,
    )


# ------------------------------------------------- reading a run back


def journal(
    journal_file: Path = typer.Argument(
        ...,
        help="What to replay: a run root, or its engine.db.",
    ),
    files: list[Path] = typer.Argument(
        None,
        help="JIL files of the run's catalog. Optional: without them the"
        " catalog is the one the run root's store recorded.",
    ),  # DL-142
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Replay a run's journal and print the reconstructed trace.

    The store records inputs only. Events and the trace are recomputed
    from them, never stored. Replay refuses when the catalog it is
    replayed against does not hash to the one the store recorded.

    The catalog argument is optional. Without it, the catalog comes
    from the run root's own store. With it, the given FILES are
    checked against the store's catalog hash. --permit-unknown and -p
    apply only to the files you give.

    Exit codes: 0 replayed; 2 refused (a catalog hash mismatch or an
    unreadable input).
    """
    # Design: runner-design ss7, DL-142, DL-424
    from dsl41.oracle_state import OracleError
    from dsl41.runner_clock import EngineError
    from dsl41.reload_diff import catalog_hash_v2
    from dsl41.runner_config import Configuration, oracle_reading
    from dsl41.runner_journal import StoreReplay, open_reader, replay_store
    from dsl41.runner_ledger import STATE_MACHINE_VERSION

    supplied = load_catalog_or_exit_2(files, permit_unknown, properties) if files else None
    try:
        store = open_reader(journal_file)
    except (OSError, EngineError) as exc:
        raise typer.Exit(refuse(exc)) from exc
    try:
        try:
            meta = store.meta()
            if meta.get("state_version") != str(STATE_MACHINE_VERSION):
                raise EngineError(
                    f"{journal_file}: state version {meta.get('state_version')}, this build"
                    f" derives {STATE_MACHINE_VERSION}: one executable replays only what its"
                    " own state machine wrote (DL-424)"
                )
            configuration = Configuration.from_bytes(
                store.get_config(store.current_config_rev()), where=str(journal_file)
            )
        except (OSError, EngineError) as exc:
            raise typer.Exit(refuse(exc)) from exc
        catalog = configuration.catalog
        if supplied is not None:
            if catalog_hash_v2(supplied) != configuration.catalog_hash:
                raise typer.Exit(
                    refuse(
                        "catalog hash mismatch: the estate differs from the one this"
                        " store ran (runner-design ss7: no silent semantic drift)"
                    )
                )
            catalog = supplied
        # SEM-35: the run's own alias table (DL-151), since a `ujo_timezones` name is site-local
        # and lives in the profile. The semantic switches (DL-252) and the base zone (DL-253)
        # come from the same profile, so the replay reads a condition as the engine that wrote
        # it did. A pruned store replays from its latest snapshot, as a restart does
        try:
            replayed: StoreReplay = replay_store(
                store, catalog, where=str(journal_file), **oracle_reading(configuration.profile)
            )
        except (OracleError, EngineError) as exc:
            raise typer.Exit(refuse(exc, prefix="replay failed")) from exc
    finally:
        store.close()
    records, replay = replayed.records, replayed.replay
    if replayed.anchor is not None:
        since = replayed.anchor.position.last_at
        stamp = since.isoformat() if since is not None else "its first record"
        typer.echo(f"trace since the snapshot at {stamp}; `dsl41 runs` has the history")
    for entry in replayed.oracle.trace():
        typer.echo(_trace_line(entry))
    for line in _rejection_lines(records, replay.decisions):
        typer.echo(line)


def _rejection_lines(records: list[dict[str, Any]], decisions: "DecisionIndex") -> list[str]:
    """The narration beside the trace: one line per engine-made input the store rejected (a
    stale completion, a quarantined input) and one per quarantine (`engine_apply_faulted`,
    concurrency-model ss4) whatever its source. Unprefixed, like the trace lines; nothing
    replayed reads it."""
    from dsl41.runner_admission import describe_input
    from dsl41.runner_journal import read_attempts

    lines = []
    for attempt in read_attempts(records):
        decided = decisions.for_index(attempt.index)
        if decided is None or decided.decision != "rejected":
            continue
        if not attempt.engine_made and decided.code != "engine_apply_faulted":
            continue
        lines.append(
            f"{attempt.at.isoformat()} rejected input {attempt.index}"
            f" ({describe_input(attempt)}): {decided.code}: {decided.reason}"
        )
    return lines


class RunsFormat(str, Enum):
    table = "table"
    json = "json"
    csv = "csv"


def _runs_table(rows: list[RunRow]) -> list[str]:
    """table format: fixed-width columns, sorted (job, started_at) by the caller, plus a labelled
    break wherever the SAME job's rows cross a definition change (`runner_history` decision 4;
    never a hidden line, never a refusal to print)."""
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
        help="Run roots (the --run-root of 'dsl41 run').",
    ),
    job: str = typer.Option(None, "--job", help="Only this job's rows."),
    since: str = typer.Option(
        None, "--since", help="Only runs started at or after this ISO 8601 instant."
    ),
    output_format: RunsFormat = typer.Option(
        RunsFormat.table,
        "--format",
        help="table: human-readable, with a marked break at every catalog"
        " change. json or csv: every field, with catalog_hash on each row.",
    ),
) -> None:
    """List the run history of one or more run roots.

    One row per job run, built from each root's journal, manifest and
    spool: when it started, how long it took, and how it ended. Command
    and file-watch runs and boxes each get rows. Rows
    sort by job and start time across all the roots given. Where the
    catalog changed between runs, the table marks the break instead of
    blending the series.

    This command reads files only. It needs no running engine and no
    socket.

    Exit codes: 0 history printed; 2 a root could not be read.
    """
    # Design: DL-113; not a control-protocol verb, which stays frozen at v3
    from dsl41.runner_history import RunHistoryError, read_run_roots

    since_at = None if since is None else _naive_utc_arg(since, "--since")
    try:
        rows = read_run_roots(list(run_roots), job=job, since=since_at)
    except RunHistoryError as exc:
        raise typer.Exit(refuse(exc)) from exc

    if any(row.fidelity == "records_only" for row in rows):
        # Loud on stderr as well as on the row, because the one degraded field that MISLEADS
        # rather than omits is `status`.
        typer.echo(
            "warning: some run roots have no stored inputs, so their rows are"
            " fidelity=records_only: no box rows, no box_name/started_by/job_hash,"
            " and a run closed by KILLJOB or term_run_time reads as RUNNING",
            err=True,
        )

    if any(row.fidelity == "partial" for row in rows):
        typer.echo(
            "warning: some run roots are pruned, so the rows of runs that began before"
            " their latest snapshot are fidelity=partial: their status comes from the"
            " snapshot and the records after it, and one that ended before it with no"
            " retained record of how reads as UNKNOWN",
            err=True,
        )

    if any(row.undecided for row in rows):
        # Same posture as the fidelity warning above (DL-156): the flag rides on every row, and
        # the stderr sentence is emitted for every format.
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
            # csv and json agree on spelling: `--format json` prints true/false, and csv.writer
            # would print Python True/False; `undecided` is the first bool column
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
