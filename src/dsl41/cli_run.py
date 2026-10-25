"""The runner verbs (DL-137): execute an estate, rehearse one, read one back.

`run` and `rehearse` drive the engine through the same `start_run`/`resume_run` path: `run` on
the wall clock with real processes, `rehearse` on the virtual clock with scripted adapters.
`journal` and `runs` are their offline readers: a WAL replayed through a fresh oracle, and run
history folded out of one or more run roots.
"""

from __future__ import annotations

import shlex
import sys
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import typer

from dsl41.ast_jil import JilFile, render_statement
from dsl41.boundary import PeriodSealed
from dsl41.cli_common import (
    PERMIT_UNKNOWN,
    PROPERTIES,
    SEMANTICS_OPT,
    TIMEZONE_MAP_OPT,
    TIMEZONE_OPT,
    SupervisorChoice,
    check_base_tz,
    confirmed_resume_refusal,
    import_tui_or_exit_2,
    load_catalog_and_ast_or_exit_2,
    load_catalog_or_exit_2,
    load_semantics,
    load_tz_aliases,
    refuse,
    resume_target_period,
    say_next,
    statechart_report,
    walk_estate_or_exit_2,
)
from dsl41.ir import CatalogIR
from dsl41.period import root_is_unused

from dsl41.state_machine import EXIT_STATECHART_ERROR, STATECHART_LOG_PREFIX, StatechartError

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable, Collection, Iterable, Sequence
    from datetime import datetime

    from dsl41.boundary import EstateWalk
    from dsl41.oracle_state import Event, TraceEntry
    from dsl41.rehearse_check import CadencePolicy
    from dsl41.runner_access import AccessControl
    from dsl41.runner_adapters import FakeAdapter, SupervisorClient
    from dsl41.runner_control import ControlServer
    from dsl41.runner_clock import VirtualClock, ZeroDelayCycleError
    from dsl41.runner_scheduler import Scheduler
    from dsl41.period import Manifest, RuntimeProfile, StagedManifest
    from dsl41.runner import Engine
    from dsl41.runner_history import RunRow
    from dsl41.runner_admission import DecisionIndex
    from dsl41.runner_preflight import PreflightItem
    from dsl41.runner_startup import SupervisorLaunch, Wiring
    from dsl41.seal import CarriedRows
    from dsl41.semantics import SemanticSwitches


# ------------------------------------------------------------------- runner
#
# Exit codes for the runner verbs: 0 clean (run: operator-stopped; rehearse: quiescent), 1 the
# engine/estate failed while running (EngineError, oracle refusal mid-run) or an attach launch found
# no supervisor to retry against (DL-396), 2 the run never started (preflight ERROR, resume gate,
# unreadable scenario, unreachable socket). The control-plane verbs (cli_control.py) read the same
# 0/1/2 and split 2 three ways. Exit 3 is per-verb (DL-184): run's 3 is a committed seal boundary;
# rehearse's 3 is --check-cadence finding deviations after a clean play. 4 is the control verbs'
# unknown outcome. 5 is reserved: deployed units list it in `RestartPreventExitStatus` (DL-353).
# run's 6 is a time-half stop (concurrency-model ss4): an engine-made input's time half raised, and
# the engine stopped before it was logged. run's and rehearse's 7 is a statechart error
# (docs/statechart-engine.md ss5): a declared machine hit an assertion failure, and the engine
# stopped before any durable write of it. EXIT_STATECHART_ERROR lives in state_machine.py, shared
# with the supervisor; the units list it in `RestartPreventExitStatus`, because a restart would run
# the same build into the same bug (deployment-runbook ss3).

#: The one stderr line `run --on-transition-violation` prints, whatever its value.
INERT_VIOLATION_OPTION = (
    "dsl41 run: --on-transition-violation is inert and is removed at"
    " the next major release; remove it from the command line"
)
#: `run`'s exit code for a time-half stop (`TimeHalfStop`). The units' `RestartPreventExitStatus`
#: lists it: a restart would meet the same timers (deployment-runbook ss3).
EXIT_TIME_HALF_STOP = 6
#: `run`'s exit code for a committed seal boundary (period-model ss7, PR-30b): a successful
#: terminal outcome, distinct from 0/1/2 so an init system does not restart-loop a sealed engine.
EXIT_SEALED = 3


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
    files: list[Path] = typer.Argument(..., help="JIL files forming the estate."),
    run_root: Path = typer.Option(
        ...,
        "--run-root",
        help="Directory for this run: the journal, runs/, logs/ and control.sock.",
    ),
    resume: bool = typer.Option(
        False, "--resume", help="Continue the run recorded in --run-root's journal."
    ),  # ss7
    open_from: Path = typer.Option(
        None,
        "--open-from",
        help="Open the next period into a fresh --run-root from the lineage this"
        " anchor directory names. The lineage head must be closed, and the"
        " closing period must be quiet and attested ('dsl41 audit'). Pass the"
        " same --estate-anchor on every later --resume.",
        # period-model ss7, ss1.1
    ),
    estate_anchor: Path = typer.Option(
        None,
        "--estate-anchor",
        help="The lineage anchor directory. Defaults to <run-root>.anchor, a"
        " sibling of the run root.",
    ),  # period-model ss1.3
    ui: bool = typer.Option(
        False, "--ui", help="Open the terminal UI in this terminal. Quitting it stops the run."
    ),  # ss11
    detached: bool = typer.Option(
        False,
        "--detached",
        help="Run command jobs under a per-run-root supervisor, so an engine"
        " restart reattaches to them instead of killing them. Stopping the"
        " engine leaves them running; resume with --resume --detached.",
    ),  # ss6a Tier 1
    deadman: float = typer.Option(
        None,
        "--deadman",
        help="Seconds without a live engine after which the supervisor exits"
        " and kills every job it holds. Needs --detached. Required for"
        " 'dsl41 host evict' without --force. Choose it longer than any"
        " planned engine outage: an engine down longer than this loses its"
        " jobs.",
        # concurrency-model ss8
    ),
    supervisor: SupervisorChoice | None = typer.Option(
        None,
        "--supervisor",
        metavar="MODE",
        help="How a --detached run gets its supervisor: attach or spawn. spawn, the default,"
        " starts one when none answers on the run root. attach connects to"
        " the one already running, such as a supervisor service, and starts"
        " none: when none answers, the run exits 1. Needs --detached.",
    ),  # DL-396
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
    timezone: str = TIMEZONE_OPT,
    timezone_map: Path = TIMEZONE_MAP_OPT,
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
) -> None:
    """Run an estate on this machine with real processes and a wall clock.

    The engine schedules jobs by calendar, starts real processes,
    journals every input, and listens on a control socket under
    --run-root. It runs until SIGINT or SIGTERM. By default jobs are
    tethered: when the engine dies, its jobs are terminated and the
    journal records it. With --detached, jobs run under a supervisor
    and survive an engine restart.

    While it runs, use 'dsl41 query' and 'dsl41 sendevent' from another
    terminal, or open the terminal UI with --ui here or 'dsl41 ui'
    elsewhere.

    Exit codes: 0 stopped by the operator; 1 the engine or the estate
    failed while running, or --supervisor attach found no supervisor; 2
    the run never started (a preflight error, a refused resume, an
    unreadable input); 3 a seal committed, and the next period is ready
    to open; 6 a time-half stop (the time half of an
    engine-made input raised, and the engine stopped before logging it);
    7 a statechart error (a declared machine hit an assertion failure).
    Exit 5 is retired and unused.
    """
    # Design: runner-design ss1, ss9, ss10, ss6a
    import asyncio

    from datetime import UTC, datetime

    if on_violation is not None:
        typer.echo(INERT_VIOLATION_OPTION, err=True)
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
    if deadman is not None and not detached:
        # loud, not silent: without a supervisor nothing holds the lifelines a deadman could bound
        # (concurrency-model ss8)
        raise typer.Exit(refuse("--deadman needs --detached: a tethered run has no supervisor"))
    if deadman is not None and deadman <= 0:
        raise typer.Exit(refuse("--deadman must be a positive number of seconds"))
    if supervisor is not None and not detached:
        raise typer.Exit(refuse("--supervisor needs --detached: a tethered run has no supervisor"))
    from dsl41.runner_clock import EngineError
    from dsl41.period import runtime_profile_from_cli

    # ss2.1: the launch options that change interpretation or dispatch, as one typed value;
    # `runtime_hash` tells a period launched --timezone UTC from one launched Europe/Zurich
    profile = runtime_profile_from_cli(
        timezone=timezone,
        tz_aliases=tz_aliases,
        as_machine=as_machine,
        machine_policy=machine_policy,
        detached=detached,
        deadman_s=deadman,
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
                    parsed=parsed,
                    anchor_dir=estate_anchor,
                    open_from=open_from,
                    access_map=access_map,
                    reattach=_reattach_line(sys.argv, _value_options(ctx.command.params)),
                    supervisor=(supervisor or SupervisorChoice.spawn).value,
                )
            )
        )
    except EngineError as exc:
        # start/resume gates (existing journal, hash/domain mismatch, live socket): never started
        raise typer.Exit(refuse(exc)) from exc


def _value_options(params: "Iterable[object]") -> frozenset[str]:
    """Every spelling of a `run` option that takes a value, so `_reattach_line` can tell an
    option's value from an input file. Read from the command's own parameters; duck-typed because
    typer ships its click as a private module."""
    names: set[str] = set()
    for param in params:
        if getattr(param, "param_type_name", None) != "option":
            continue
        if getattr(param, "is_flag", False) or getattr(param, "count", False):
            continue
        names.update(getattr(param, "opts", ()), getattr(param, "secondary_opts", ()))
    return frozenset(names)


def _reattach_line(argv: "Sequence[str]", value_options: "Collection[str]") -> str | None:
    """The shell-quoted command that reattaches this detached run, from the argv that started it
    (DL-218); None when `argv` is not a `run` invocation.

    A resume needs the whole original line (files in order, every `-p`, the runtime profile and
    `--access-map`; deployment-runbook ss5), so every word stays in place with three edits.
    `--open-from X` becomes `--estate-anchor X` (the openers are exclusive and the anchor is the
    lineage's; period-model ss7); where `--estate-anchor` is present, the `--open-from` words are
    dropped. `--resume` and `--detached` are each present exactly once: the first spelling kept,
    a missing one added after `run`."""
    if len(argv) < 2 or argv[1] != "run":
        return None
    # `python -m dsl41` leaves the package's __main__.py in argv[0]
    prog = [sys.executable, "-m", "dsl41"] if Path(argv[0]).name == "__main__.py" else [argv[0]]
    words = list(argv[2:])
    items: list[tuple[str | None, list[str]]] = []  # (option name or None, its words)
    at = 0
    while at < len(words):
        word = words[at]
        if word == "--":  # everything after it is an input
            items.append((None, words[at:]))
            break
        if word.startswith("--") and "=" in word:
            items.append((word.split("=", 1)[0], [word]))
        elif word in value_options:
            items.append((word, words[at : at + 2]))
            at += 1
        elif word[:1] == "-" and word[1:2] != "-" and word[:2] in value_options:
            items.append((word[:2], [word]))  # a short option with its value attached
        elif word.startswith("-"):
            items.append((word, [word]))
        else:
            items.append((None, [word]))
        at += 1
    has_anchor = any(name == "--estate-anchor" for name, _ in items)
    kept: list[str] = []
    seen: set[str] = set()
    for name, item in items:
        if name == "--open-from":
            if has_anchor:
                continue
            item = ["--estate-anchor" + item[0][len("--open-from") :], *item[1:]]
        elif name in ("--resume", "--detached"):
            if name in seen:
                continue
            seen.add(name)
        kept.extend(item)
    missing = [flag for flag in ("--resume", "--detached") if flag not in seen]
    return shlex.join([*prog, "run", *missing, *kept])


def _reattach_note(
    reattach: str | None, run_root: Path, *, detached: bool, code: int
) -> str | None:
    """The line a detached run prints on its way out, or None (DL-218).

    Only a detached run that can be resumed gets one: a clean stop (0), a crash (1), a time-half
    stop (6) or a statechart error (7), the last two resuming on a fixed build. A sealed exit
    (`EXIT_SEALED`) gets none; `say_next` names the next opener. `reattach` is this process's own
    line; an embedding caller without one gets the schematic."""
    if not detached or code not in (
        0,
        1,
        EXIT_TIME_HALF_STOP,
        EXIT_STATECHART_ERROR,
    ):
        return None
    line = reattach or f"dsl41 run --resume --detached --run-root {run_root} <files>"
    return f"detached: reattach with `{line}`"


def _observed_profile(
    staged: "StagedManifest | None", running_deadman: "float | None"
) -> "StagedManifest | None":
    """Re-pin the runtime profile on the deadman the run REALLY has.

    A supervisor already up keeps its interval, so `--deadman 90` against one running 60 starts
    the engine with 60 and the manifest must say 60 (DL-126, as `_running_deadman`)."""
    from dsl41.period import runtime_hash, to_us

    if staged is None:
        return None
    observed = None if running_deadman is None else to_us(running_deadman)
    if observed == staged.runtime_profile.deadman_us:
        return staged
    # the staged manifest carries BOTH the profile and its hash, so a re-pinned deadman moves the
    # hash with it; the two wiring sites below take the profile alone
    profile = staged.runtime_profile.with_deadman(observed)
    return staged.model_copy(
        update={"runtime_profile": profile, "runtime_hash": runtime_hash(profile)}
    )


def _resume_profile_error(
    run_root: Path, profile: "RuntimeProfile", running_deadman: "float | None"
) -> "str | None":
    """PR-22's runtime half (period-model ss2.1): a period's semantics are (catalog_hash,
    runtime_hash, state_machine_version), and either of the first two moving is a new period.

    The catalog gate lives in resume; this holds the LAUNCH OPTIONS to the pin. A root with no
    manifest has no pin to hold (DL-130). The deadman compares at its OBSERVED value."""
    from dsl41.period import read_period_manifest, to_us
    from dsl41.runner_clock import EngineError

    try:
        # the manifest of the period this resume will OPEN INTO, never period 1's: a rolled root
        # holds only the period it was opened into (DL-134), and a root with a committed boundary
        # is about to move to the next one (DL-151)
        manifest = read_period_manifest(run_root, resume_target_period(run_root))
    except EngineError as exc:
        return str(exc)
    if manifest is None:
        return None
    observed_deadman = None if running_deadman is None else to_us(running_deadman)
    return _profile_mismatch(profile.with_deadman(observed_deadman), manifest)


def _profile_mismatch(observed: "RuntimeProfile", manifest: "Manifest") -> "str | None":
    """The ONE comparison of a launch's options with a period's pin, and its refusal.

    The resume gate runs it after wiring, at the observed deadman. A physical roll also runs it
    before it writes, at the pinned deadman (PR-22b)."""
    from dsl41.period import runtime_hash
    from dsl41.runner_startup import profile_drift

    if runtime_hash(observed) == manifest.runtime_hash:
        return None
    # names only: the caller reports WHICH options moved, by the core's drift walk (DL-137)
    moved = profile_drift(observed, manifest.runtime_profile)
    return (
        "runtime-profile mismatch: this run was launched with different"
        f" options than the period pinned ({', '.join(moved) or 'runtime_hash'})."
        " A runtime-profile change is a new period (period-model ss2.1):"
        " re-baseline explicitly with a fresh run root"
    )


def _roll_admission(run_root: Path, profile: "RuntimeProfile") -> "Callable[[Manifest], None]":
    """The gate a physical roll holds its successor's manifest to before it writes (PR-22b);
    raises `EngineError` with `_profile_mismatch`'s refusal.

    The deadman is compared at the value the run will observe. A tethered run has none. A detached
    run with no supervisor at the target starts one at the pinned deadman; under `--supervisor
    attach` it stops at the wiring and starts none (DL-396). One already running keeps its own
    interval, so the gate asks it, read-only, after the local comparison passes."""
    from dsl41.period import to_us
    from dsl41.runner_adapters import running_supervisor_deadman
    from dsl41.runner_clock import EngineError

    detached = profile.execution_mode == "detached"

    def admit(manifest: "Manifest") -> None:
        pinned = manifest.runtime_profile.deadman_us if detached else None
        mismatch = _profile_mismatch(profile.with_deadman(pinned), manifest)
        if mismatch is None and detached:
            # asked on every call, so the roll's own call observes the supervisor right before its
            # first write
            answered, running = running_supervisor_deadman(run_root)
            if answered:
                observed = None if running is None else to_us(running)
                mismatch = _profile_mismatch(profile.with_deadman(observed), manifest)
        if mismatch is not None:
            raise EngineError(mismatch)

    return admit


def _running_deadman(
    client: object, asked: "float | None", run_root: Path, resume: bool, pinned: "float | None"
) -> "float | None":
    """The deadman the LOCAL SUPERVISOR reports it runs (concurrency-model ss8), and a warning
    when that is not what this invocation asked for.

    It is read back, not assumed: the eviction bound has to describe the host, and a number in
    the routing table that names nothing is the length of the wait before a double run."""
    from dsl41.period import to_us

    running = getattr(client, "supervisor_deadman_s", None) if client is not None else None
    if asked is not None and running != asked:
        stop = (
            f" With the engine stopped, stop it: `dsl41 supervise shutdown --run-root"
            f" {shlex.quote(str(run_root))}`. Caveats: docs/deployment-runbook.md section 3."
        )
        head = f"note: the supervisor serving {run_root} runs deadman {running!r}, not the "
        if not resume:
            text = head + f"{asked} asked for -- it was already up." + stop
        elif (running if running is None else to_us(running)) == (
            pinned if pinned is None else to_us(pinned)
        ):
            # the period pins its deadman (period-model ss2.1): the resume starts a missing
            # supervisor at the pin, so stopping this one changes nothing
            text = (
                head + f"{asked} asked for. A resumed period keeps the deadman it pinned; it"
                " changes at the next boundary (`dsl41 seal --next-deadman`,"
                " docs/deployment-runbook.md section 6a)."
            )
        else:
            # a supervisor that outlived the last boundary keeps its old interval, and the
            # resume gate refuses it against the pin
            text = head + f"{pinned} the period pinned -- it was already up." + stop
        typer.echo(text, err=True)
    return running


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


def _preflight_launch(
    run_root: Path,
    profile: "RuntimeProfile",
    open_from: "Path | None",
    access_map: "Path | None",
) -> "Callable[[Manifest], None] | None":
    """`run`'s read-only checks, before anything is created: the roll's preflight, the access
    policy's load, and the roll's admission gate. Returns that gate (None without a roll). A
    failing check raises the `EngineError` or `AccessError` it met."""
    admit: "Callable[[Manifest], None] | None" = None
    if open_from is not None:
        # the roll's READ-ONLY preflight runs first: a refusal, above all the unattested-closing
        # one, must write nothing, not even the target directory and its lock. Every gate re-runs
        # authoritatively inside the roll under the locks.
        from dsl41.estate import check_roll_ready

        successor = check_roll_ready(run_root, Path(open_from))
    if access_map is not None:
        # the access-model ss4 refusal must write NOTHING: the loader arming runs later is called
        # here read-only, before the root is claimed. `socket_group` resolution is the loader's own
        # (DL-152), so an unknown group refuses here in the loader's words
        from dsl41.runner_access import load_policy

        load_policy(access_map, generation=1)
    if open_from is not None:
        # PR-22b: the options are held to the successor's pin before the roll writes. This pass
        # runs after every local check because it may PING a supervisor; the roll runs the same
        # gate again on the seal it selects.
        admit = _roll_admission(run_root, profile)
        admit(successor)
    return admit


def _install_signal_handlers(stop: "asyncio.Event", access: "AccessControl | None") -> None:
    """Route SIGINT and SIGTERM to `stop`, and SIGHUP to the access reload."""
    import asyncio
    import signal as signal_mod

    loop = asyncio.get_running_loop()
    for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
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


class _RunEnd(NamedTuple):
    """What `_await_run_end` hands `_stop_and_judge`: the loop task, the attached TUI and its
    task, and the tasks that finished."""

    loop_task: "asyncio.Task[Any]"
    tui: Any
    ui_task: "asyncio.Task[Any] | None"
    done: "set[asyncio.Task[Any]]"


async def _await_run_end(
    engine: "Engine",
    server: "ControlServer",
    client: "SupervisorClient | None",
    stop: "asyncio.Event",
    ui: bool,
) -> _RunEnd:
    """Start the engine loop and wait for the first thing that ends the run: the loop, a stop
    signal, the server's failure or the attached TUI."""
    import asyncio
    from datetime import datetime

    loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    # a control handler's StatechartError stops the run as one from the loop does
    # (statechart-engine ss5): the handler cancels the loop at once and the run wakes on `failed`
    server.on_statechart_error = loop_task.cancel
    if client is not None:
        # the same stop for one the supervisor client's background tasks meet (reader, renewal,
        # LIST net); one met before the loop began is handed over now
        client.on_statechart_error = server.stop_on
    stop_task = asyncio.ensure_future(stop.wait())
    failed_task = asyncio.ensure_future(server.failed.wait())
    ui_task: asyncio.Task[Any] | None = None
    tui = None
    if ui:
        tui, ui_task = _attach_tui(server.path, owns_run=True)
    waiters = {loop_task, stop_task, failed_task}
    waiters |= {ui_task} if ui_task is not None else set()
    done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    stop_task.cancel()
    failed_task.cancel()
    return _RunEnd(loop_task, tui, ui_task, done)


async def _stop_and_judge(
    engine: "Engine",
    server: "ControlServer",
    end: _RunEnd,
    *,
    detached: bool,
) -> int:
    """How the run ended: tear down the TUI, the server and the engine; return the exit code
    (`EXIT_SEALED` when a seal boundary committed)."""
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
        # detach-stop (spec ss3 case b): teardown must NOT kill jobs; the flag makes the
        # SupervisedCommandAdapter abandon its await instead of signaling. Set before any
        # adapter-task cancel.
        if detached:
            engine.detach.stopping = True
        # how the loop ended, if on its own; a loop the server cancelled ended for the server's
        # error, read below
        ended = loop_task in done and not loop_task.cancelled()
        outcome = loop_task.exception() if ended else None
        if isinstance(outcome, PeriodSealed):
            # ss7: a committed boundary is a SUCCESSFUL terminal outcome with its own code,
            # distinct from 0/1/2 (no init-system restart loop) and from the crash branch below,
            # which `hold_open` makes the only other way this loop returns. Detached work is NOT
            # signalled: `detach.stopping` is set above, so the supervised adapter abandons its
            # await and the next period reattaches (PR-30b). `say_next` runs after the teardown.
            typer.echo(str(outcome))
            code = EXIT_SEALED
        elif isinstance(outcome, TimeHalfStop):
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
        elif ended:  # hold_open never quiesces: this is a crash
            typer.echo(f"engine failed: {outcome}", err=True)
            code = 1
        else:
            # operator stop: a signal, or quitting the attached TUI (ss11: --ui tethers the run to
            # this terminal; `dsl41 ui` attaches viewers that must not stop it)
            if tui_failure is not None:
                typer.echo(f"TUI failed: {tui_failure}", err=True)
                code = 1
            if detached:
                typer.echo("stopping: jobs continue under the supervisor (detached, ss6a)")
            else:
                typer.echo("stopping: cancelling live jobs (wrappers record the kills, ss6a)")
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
    run_root: Path,
    anchor_dir: "Path | None",
    reattach: "str | None",
    *,
    supervised: bool,
) -> int:
    """After the lease went back: judge the server's statechart error against the exit code,
    name the next step of a sealed run and print the reattach note. Returns the exit code."""
    if server.statechart_error is not None and code != EXIT_STATECHART_ERROR:
        # recorded while a loop outcome took precedence, met while the server closed, or met
        # while the lease went back. A crash or a clean stop becomes 7; a halt the engine chose
        # keeps its code
        statechart_report(server.statechart_error)
        if code in (0, 1):
            code = EXIT_STATECHART_ERROR
        else:
            typer.echo(
                f"{STATECHART_LOG_PREFIX}: the engine had already stopped with exit {code}",
                err=True,
            )
    if code == EXIT_SEALED:
        say_next(run_root, anchor_dir)
    # a client exists exactly when the profile said detached; `_serve_run` gave the lease back
    note = _reattach_note(reattach, run_root, detached=supervised, code=code)
    if note is not None:
        typer.echo(note)
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
    parsed: "list[JilFile] | None" = None,
    anchor_dir: "Path | None" = None,
    open_from: "Path | None" = None,
    access_map: "Path | None" = None,
    reattach: "str | None" = None,
    supervisor: "SupervisorLaunch",
) -> int:
    """`dsl41 run`, from the acquire to the last teardown.

    The profile is REQUIRED and is the one source of every launch option read here (timezone,
    alias table, tethered-vs-detached, the asked deadman, every adapter window); a `detached` flag
    beside a `tethered` profile would wire a supervised adapter and tear down as if none existed
    (DL-137)."""
    import asyncio

    from datetime import UTC, datetime

    from dsl41.boundary import stage_period
    from dsl41.period import to_us
    from dsl41.runner_startup import SupervisorAbsent, start_run, wire_from_profile
    from dsl41.runner_control import ControlServer
    from dsl41.runner_startup import resume_run as _resume_run
    from dsl41.runner_access import AccessError
    from dsl41.runner_clock import EngineError, RealClock

    from dsl41.runner_ledger import acquire_run_root

    clock = RealClock()
    detached = profile.execution_mode == "detached"
    # the ASKED deadman; `_running_deadman` reads back what the host runs
    deadman = None if profile.deadman_us is None else profile.deadman_us / 1_000_000
    try:
        admit = _preflight_launch(run_root, profile, open_from, access_map)
    except (EngineError, AccessError) as exc:
        return refuse(exc)
    # ACQUIRE first (S6a, concurrency-model ss7): the next step starts or attaches to a supervisor
    # and takes its lease, an act on an estate this process may not lead
    try:
        lock = acquire_run_root(run_root)
    except EngineError as exc:
        return refuse(exc)
    # ONE teardown, for every way out (DL-145). The supervisor's lease and the leader lock are
    # taken here and given back here, in the order lease, journal, lock; `release` is idempotent,
    # so the S6a close inside `journal.close` stays where the log ends. A lease abandoned INSIDE
    # `wire_from_profile` is that function's to give back.
    wiring: "Wiring | None" = None
    engine: "Engine | None" = None
    try:
        if resume:
            # ss1.3's resume rule (DL-224), before anything is staged and before the supervisor
            # wiring creates its log, lock, socket and pid file: a refused resume would leave that
            # supervisor running. Exit 2 tells the units never to restart, so a refusal from a
            # stale snapshot must not stand; the helper confirms it under the anchor lock.
            refusal = confirmed_resume_refusal(run_root, anchor_dir)
            if refusal is not None:
                return refuse(refusal)
        # stage period 1 UNDER the lock (period-model ss1.1): a used run root is start_run's
        # refusal to make, and what a refusal leaves behind is content-addressed and never read
        # (residue the spec tolerates). OWNERSHIP first, the FULL ss1.1 predicate: a sentinelless
        # root that keeps a WAL, a seal, a committed period or a populated runs/ is somebody's
        # work, and staging and the supervisor start are acts on an estate this process may not
        # lead.
        if open_from is not None:
            # ss7's second opener, in order: new-root leader.lock (above), sentinel durable,
            # anchor.lock and the claim, the import, the segment, the head. What comes back is an
            # ordinary period-N root and the ladder below resumes it (PR-07).
            from dsl41.estate import check_roll_target, roll_into_root

            try:
                check_roll_target(run_root, open_from)
                rolled = roll_into_root(
                    run_root,
                    anchor_dir=open_from,
                    catalog_of=lambda _root, _m: catalog,
                    lock=lock,
                    admit=admit,
                )
            except EngineError as exc:
                return refuse(exc)
            # stderr: stdout's first line is the `engine up` handshake supervisors and tests wait
            # for, and a roll note ahead of it would move it
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
            # start a MISSING supervisor with the deadman the period PINNED, not the one asked:
            # asking 90 against a pinned 60 would start a 90-second supervisor before the profile
            # gate refuses, and the next correct 60-second resume would observe 90 and refuse too.
            # The ask still warns (_running_deadman) and still refuses below if it differs.
            from dsl41.period import read_period_manifest

            try:
                pinned = read_period_manifest(run_root, resume_target_period(run_root))
            except EngineError as exc:
                return refuse(exc)
            if pinned is not None:
                pinned_us = pinned.runtime_profile.deadman_us
                supervisor_deadman = None if pinned_us is None else pinned_us / 1_000_000
        # ONE wiring builder (DL-137): genesis, resume and the offline sealer build adapters and
        # scheduler from the PROFILE, so a window that moves on the pin moves in the components
        # that run. The deadman is the single field re-pinned here.
        pinned_deadman_us = None if supervisor_deadman is None else to_us(supervisor_deadman)
        try:
            wiring = await wire_from_profile(
                run_root,
                catalog,
                profile.with_deadman(pinned_deadman_us),
                start=datetime.now(UTC).replace(tzinfo=None),
                supervisor=supervisor,
            )
        except SupervisorAbsent as exc:
            # exit 1, not a refusal's 2: the engine unit retries it behind `Requires=` until its
            # supervisor unit answers (DL-396)
            typer.echo(
                f"{exc}; --supervisor attach starts none: start the supervisor service", err=True
            )
            return 1
        except EngineError as exc:
            return refuse(exc)
        client = wiring.client
        adapters = wiring.adapters
        scheduler = wiring.scheduler
        running_deadman = _running_deadman(client, deadman, run_root, resume, supervisor_deadman)
        staged = _observed_profile(staged, running_deadman)
        if resume:
            error = _resume_profile_error(run_root, profile, running_deadman)
            if error is not None:
                return refuse(error)
        try:
            if resume:
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
                    # the launch options no wired object can report (machine identity and policy):
                    # without them the core gate inherits them from the pin and can never
                    # disagree with it, so a boundary that staged a new identity would open under
                    # the old one (DL-151)
                    declared=profile,
                    machines=wiring.machines,
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
                    launched=profile,
                    machines=wiring.machines,
                )
        except StatechartError as exc:
            # a replayed input that meets one: the report names the WAL input, and exit 7 keeps
            # the unit down (docs/statechart-engine.md ss5)
            return statechart_report(exc)
        if client is not None:
            # ss8's supervisor clauses at the seal (PR-27): the CLIENT must prove the LIST it
            # reconciles came from the leased incarnation, so the engine holds it, not only the
            # adapter.
            engine.supervisor = client
            # ss8's "positive contact with this host": every confirmed lease exchange stamps the
            # routing row (S5b). Wired after the engine exists; the genesis seed stamps the first
            # ACQUIRE's instant.
            client.on_contact = engine.note_executor_contact
            # ss8: a host the leader cannot reach is quarantined, so new work is HELD until it
            # answers; the reinstate rides on the next confirmed contact.
            client.on_unreachable = engine.note_executor_unreachable
        # everything resume did not apply: E9's missed scheduler ticks, plus any reconciliation
        # completion the ss4 gate rejected; both are on `drops` (DL-91)
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
        # Handlers go in BEFORE the socket is bound, so a SIGHUP after this engine bound
        # control.sock is a reload, not a kill. A signal before the handlers exist keeps its
        # default action; one before the loop task exists only sets `stop` or reloads.
        stop = asyncio.Event()
        _install_signal_handlers(stop, access)
        try:
            await server.start()
        except EngineError as exc:
            # the handlers stay until `loop.close()`, as on the success path
            return refuse(exc)
        typer.echo(f"engine up; control socket: {server.path}")
        end = await _await_run_end(engine, server, client, stop, ui)
        code = await _stop_and_judge(engine, server, end, detached=detached)
        # the lease goes back before the run's error is judged: the client's reader is live
        # during RELEASE and close, and a statechart error it meets there reaches the server.
        # Taken off `wiring` first so the teardown does not give it back twice
        returning, wiring = wiring, None
        try:
            await returning.close()
        finally:
            # judged, and the next step named, whatever the return raised; the CLI wrapper
            # reports a statechart error as exit 7
            code = _finish_run(
                server, code, run_root, anchor_dir, reattach, supervised=client is not None
            )
        return code
    finally:
        # EACH step guaranteed, whatever the one before it did (DL-145): the supervisor's lease,
        # the log's fsync and the estate's leader lock are three obligations to three things. A
        # failing fsync inside `journal.close()` must PROPAGATE, but must not also strand the
        # lock, which would refuse the operator's retry with "held by another engine". Nesting
        # lets the first raiser out without letting it skip the rest.
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
    parsed: "list[JilFile]",
    run_root: "Path | None",
    clock: "VirtualClock",
    adapters: "dict[str, FakeAdapter]",
    scheduler: "Scheduler",
    switches: "SemanticSwitches",
    *,
    timezone: "str | None",
    tz_aliases: "dict[str, str] | None",
    overrides: "dict[str, str]",
) -> "Engine":
    """The engine `rehearse` plays: the genesis of `run_root` when one was
    named, else a bare in-memory engine. An `EngineError` is a refusal."""
    from dsl41.boundary import stage_period
    from dsl41.period import runtime_profile_from_cli
    from dsl41.runner import Engine
    from dsl41.runner_clock import EngineError
    from dsl41.runner_startup import start_run

    try:
        if run_root is not None:
            # a rehearsal's run root is a self-contained artifact: the profile it interpreted the
            # estate under, its timezone above all, belongs in the manifest or the log claims a
            # period it did not run (ss2.1). Staged only for a FRESH root; an existing journal is
            # start_run's refusal to make.
            launched = runtime_profile_from_cli(
                timezone=timezone, tz_aliases=tz_aliases, semantics=overrides
            )
            staged = (
                stage_period(run_root, parsed, catalog, launched)
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
                # a root holding only the sentinel stages nothing; genesis then pins the launch
                # options, every switch included
                launched=launched,
            )
        else:
            engine = Engine(
                catalog,
                clock=clock,
                adapters=adapters,
                scheduler=scheduler,
                semantics=switches,
            )
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc
    return engine


def rehearse(
    files: list[Path] = typer.Argument(..., help="JIL files forming the estate."),
    scenario: Path = typer.Option(
        None,
        "--scenario",
        help="JSON scenario with scripted job outcomes and events to inject."
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
    timezone: str = TIMEZONE_OPT,
    timezone_map: Path = TIMEZONE_MAP_OPT,
    semantics: list[str] = SEMANTICS_OPT,
    run_root: Path = typer.Option(
        None, "--run-root", help="Also write the journal under this directory."
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    r"""Play an estate under a virtual clock and print what would happen.

    Rehearsal runs the same engine as 'run' with scripted job outcomes,
    so a 24-hour estate plays in seconds. The printed trace shows what
    production would do.

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
    catalog, parsed, _ = load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)
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
        parsed,
        run_root,
        clock,
        adapters,
        scheduler,
        switches,
        timezone=timezone,
        tz_aliases=tz_aliases,
        overrides=overrides,
    )
    if warns and engine.journal is not None:
        engine.journal.preflight(warns)
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
        help="What to replay: a run root, its journal.jsonl, or one"
        " wal/NNNNNN.jsonl segment. A root or journal.jsonl replays every"
        " retained segment in period order. A segment replays that period"
        " only. The lineage anchor directory replays every root in the"
        " registry.",
    ),
    files: list[Path] = typer.Argument(
        None,
        help="JIL files of the first replayed period's catalog. Optional:"
        " without them each period's catalog is loaded from the estate's"
        " bundle.",
    ),  # DL-142
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Replay a run's journal and print the reconstructed trace.

    The journal stores inputs only. Events and the trace are recomputed
    from them, never stored. Replay refuses when the journal's catalog
    hash does not match the catalog it is replayed against.

    Replay crosses period boundaries. At each segment record it folds
    state through the seal, loads the next period's catalog from the
    bundle the segment pins, and continues. Every seal is checked before
    it is crossed: its digest, its fields against the sidecar, its
    chain, and its re-derivation from the period's evidence or its
    predecessor's attestation. A seal that fails any check refuses the
    replay.

    The catalog argument is optional. Without it, every period's
    catalog comes from the estate's own bundle. With it, the given
    FILES are the first replayed period's catalog, checked against its
    pinned hash; later periods still come from their bundles.
    --permit-unknown and -p apply only to the files you give.

    Given the lineage anchor directory, replay covers the whole estate:
    every root in the registry, in period order, across physical rolls.

    Exit codes: 0 replayed; 2 refused (a catalog hash mismatch, a seal
    that fails its checks, or an unreadable input).
    """
    # Design: runner-design ss7, period-model ss11, DL-142, ss2.2, DL-130,
    # ss1.3, PR-02f
    from dsl41.boundary import is_anchor_dir

    catalog = load_catalog_or_exit_2(files, permit_unknown, properties) if files else None
    if is_anchor_dir(journal_file):
        _replay_estate(walk_estate_or_exit_2(journal_file), catalog)
        return
    _replay_lineage(_segments_named(journal_file), catalog)


def _segments_named(target: Path) -> list[Path]:
    """Which segments the caller meant by one path.

    A ROOT or a sentinel means the whole root, every period it retains, in order (the DL-136
    rule, as `dsl41 runs`): resolving a root to its ACTIVE segment answers about the current night
    only. A `wal/NNNNNN.jsonl` names exactly one period."""
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
        # a root with no periodized sentinel answers with `journal.jsonl` itself, whose stem is
        # not a period number; no lineage can have archived anything, and the reader below
        # refuses it by name (a numeric sort would raise a traceback instead)
        return list(held)
    # an ARCHIVED period retains no file and is still a period of this root; naming its segment
    # makes the replay ANNOUNCE the unreplayable gap instead of answering with a shorter lineage
    # that looks whole (DL-144)
    root = root_of_wal(resolved)
    gone = {wal_path(root, number) for number in archived_periods(root)}
    gone |= {wal_path(root, number) for number in _lost_below(root, held)}
    return sorted({*held, *gone}, key=lambda path: int(path.stem))


def _lost_below(root: Path, held: "Sequence[Path]") -> list[int]:
    """Period numbers BELOW the oldest segment this root retains for which the root still holds a
    committed period manifest (DL-144).

    Genesis and every in-place opening write `periods/<N>/manifest.json` into the root that ran
    period N, while a ROLLED root imports only the successor's. A manifest here with no segment
    and no receipt is THIS root's period, gone, which `_announce_gap` refuses as loss. Only BELOW
    the oldest retained segment: archives are a prefix (ss12a), and a COMMITTED-NEXT period has a
    manifest and no segment yet, always above."""
    from dsl41.period import wrote_period

    if not held:
        return []
    oldest = min(int(path.stem) for path in held)
    return [number for number in range(1, oldest) if wrote_period(root, number)]


def _replay_estate(walk: "EstateWalk", catalog: "CatalogIR | None") -> None:
    """Every segment the registry names, in period order, replayed as one lineage.

    The enumeration comes first and completes: each period, its root and segment file, whether or
    not the replay reaches it. Replaying inside that loop put the whole listing behind one
    segment's gate. What is named is what is replayed, the walk's rows rather than the disk's."""
    from dsl41.period import wal_path

    for entry in walk.periods:
        segment = wal_path(entry.root, entry.period_id)
        archived = ""
        if entry.archived is not None:
            # two different disks, two different words: the receipt says the period is archived
            # either way, and only the missing file makes it unreplayable
            archived = (
                "  [inputs archived, still on disk]"
                if segment.is_file()
                else "  [inputs archived: unreplayable]"
            )
        typer.echo(f"period {entry.period_id} in {entry.root}: {segment}{archived}")
    # LABELLED even for a one-period lineage: an estate-wide answer names the period it is about
    _replay_lineage(
        [wal_path(entry.root, entry.period_id) for entry in walk.periods],
        catalog,
        labelled=True,
    )


def _label(segment: Path) -> str:
    """`period N in ROOT` for one segment file, read off the layout alone (I1: segment N is
    period N).

    Only a MANY-segment read is labelled; its lists come from `estate_segments` or the walk, which
    spell `wal/NNNNNN.jsonl` only. A root with no `wal/` yields ONE file, never seen here."""
    from dsl41.period import root_of_wal

    return f"period {int(segment.stem)} in {root_of_wal(segment)}"


def _replay_lineage(
    segments: "Sequence[Path]", catalog: "CatalogIR | None", *, labelled: bool = False
) -> None:
    """One lineage's segments, replayed as ONE run of the state machine.

    Each segment gets a fresh interpreter opened from the seal the segment names (the
    `open_from_seal` fold an engine performs, period-model ss7 phase 3) under the catalog its own
    bundle holds, and the traces concatenate.

    Every boundary is PROVED before it is crossed: the segment sits where it says
    (`check_segment_identity`), the closing seal record is the one the newer segment opens from,
    with one estate and a continuous index frontier (`check_segment_adjacency`), and the sidecar
    is the seal that opening stands on (`prove_opening`). Refuse-don't-degrade applies to a
    diagnosis surface as to an engine (DL-139).

    The crossing is announced only once it has happened: the period is OPENED first (catalog and
    carry can refuse) and the line prints after. It goes to STDOUT with the trace, so a reader
    who dropped stderr does not get two periods concatenated with no seam.

    `labelled` forces the per-period prefix on a single-period estate-wide walk."""
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
    previous: list[dict[str, Any]] | None = None
    previous_segment: Path | None = None
    # the caller's catalog gates the first period this read actually REPLAYS, which is not list
    # slot 0 once a gap can sit in front of it (DL-144)
    first = True
    for position, segment in enumerate(segments):
        where = _label(segment) if labelled else ""
        if _periodized(segment) and not segment.is_file():
            # an ARCHIVED period, or a lost one. `_announce_gap` refuses unless a receipt licenses
            # the absence, and otherwise prints the gap and drops `previous`, so the NEXT boundary
            # crosses by its predecessor's attestation rather than a re-derivation with no
            # evidence (DL-142, DL-144). A path that is NOT `wal/NNNNNN.jsonl` names no period and
            # falls through to `read_journal`, which refuses it by name
            _announce_gap(segment, where=where)
            previous, previous_segment = None, None
            continue
        try:
            records = read_journal(segment)
            if position < len(segments) - 1:
                # a segment is CLOSED by its place in the list it was given, which is known before
                # it replays, so a torn tail refuses ahead of the trace (`check_segment_tail`)
                check_segment_tail(segment, records)
            check_segment_identity(segment, records[0])
            # period-model ss2.1: a foreign state machine cannot lead OR replay, and the
            # resume-side gate never runs here. Checked HERE, before the crossing is proved and
            # announced, so no refusal follows a stated crossing
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
        # ss7 phase 3, offline: what this period opened with. Nothing prints between the two, so
        # a boundary that cannot be opened is never announced as opened.
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
            records, opened, where=where, profile=_period_profile(root, records[0], where=where)
        )
        previous, previous_segment = records, segment


def _periodized(segment: Path) -> bool:
    """Whether this path is a `wal/<six digits>.jsonl` that NAMES a period.

    A root with no periodized sentinel resolves to `journal.jsonl` itself (period-model ss1.1,
    DL-138), which has no period number, so the readers refuse it by name instead of raising out
    of an `int()`. Answered by round-tripping the layout's own helpers (DL-145): a path is period
    N's segment when the owner, given its root, names this path for N."""
    from dsl41.period import root_of_wal, wal_path

    if not segment.stem.isdigit():
        return False
    return wal_path(root_of_wal(segment), int(segment.stem)) == segment


def _announce_gap(segment: Path, *, where: str) -> None:
    """One archived period, named on STDOUT as an unreplayable gap (period-model ss12, DL-144).

    On stdout with the trace, on DL-142's rule: a reader who dropped stderr would get the periods
    on either side concatenated with no seam. A segment that is simply GONE refuses; the receipt
    must LICENSE this very file, and a receipt that archived only a candidate pair does not excuse
    a WAL that went missing by accident."""
    from dsl41.attest import verify_archive_receipt
    from dsl41.period import archive_receipt_path, root_of_wal
    from dsl41.runner_clock import EngineError

    root = root_of_wal(segment)
    period_id = int(segment.stem)
    try:
        # the ONE door (attest.py): a receipt that does not prove out is not a licence
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
    """An archived period whose segment is STILL on disk, named before it is replayed (DL-144).

    That is the crash window between the receipt and the deletions, or a restored file; the
    archive is irreversible, so the period reads at the attestation-verified tier. The inputs may
    still be READ, so this narrates rather than refuses, and the trace agrees with `audit`."""
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


def _prove_crossing(
    root: Path, opening: dict[str, Any], *, predecessor: "Path | None", where: str
) -> None:
    """ss11's "verified means RE-DERIVED, not self-consistent", asked of the seal this period
    opens from, before the period is opened and the crossing announced.

    Every other check on this path proves integrity and binding, which a forger who rewrites the
    sidecar canonically and copies its digest into the closing `seal` record and the opening
    passes. Which of two proofs applies depends on what this read holds:

    * the predecessor's evidence is being replayed (the ordinary lineage walk, in place or
      across a roll): `attest.prove_derived` rebuilds the predecessor seal from its WAL, spool and
      manifests in the root that HOLDS them and refuses when the stored sidecar differs.
      `rederive_seal` also runs `check_record_names_sidecar`, so a rewritten `seal` RECORD over an
      honest sidecar refuses too, naming the fields.
    * a later segment was named ALONE: nothing is re-derivable, so the attestation (ss11) is
      required and its absence is a refusal that names it.

    An unpruned lineage replays each period twice, once to re-derive its seal and once to narrate
    it."""
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


def _period_profile(root: Path, opening: "dict[str, Any]", *, where: str) -> "RuntimeProfile":
    """This period's runtime profile, from a manifest bound to its segment (period-model ss2.1,
    PR-22): the SEM-35 alias table and the semantic switches a replay must read as the engine did.

    Required, not degraded (DL-252): a missing or foreign manifest refuses."""
    from dsl41.runner_history import RunHistoryError, pinned_profile

    try:
        return pinned_profile(root, opening)
    except RunHistoryError as exc:
        raise typer.Exit(refuse(exc, prefix=where)) from exc


def _run_period(
    records: list[dict[str, Any]],
    opened: "tuple[CatalogIR, CarriedRows | None]",
    *,
    where: str,
    profile: "RuntimeProfile | None" = None,
) -> None:
    """ONE period, replayed and printed; a single-segment read and a lineage differ in which
    segments they name, never in what replaying one means."""
    from dsl41.oracle import Oracle
    from dsl41.oracle_state import OracleError
    from dsl41.period import oracle_reading
    from dsl41.runner_clock import EngineError
    from dsl41.runner_journal import replay_period

    catalog, carried = opened
    named = f"{where}: " if where else ""
    # SEM-35: the period's own alias table (DL-151), since a `ujo_timezones` name is site-local
    # and lives in the pin. The semantic switches (DL-252) and the base zone (DL-253) come from
    # the same pin, so the replay reads a condition as the engine that wrote the log did.
    oracle = Oracle(catalog, carried=carried, **oracle_reading(profile))
    try:
        replay = replay_period(oracle, records)
    except (OracleError, EngineError) as exc:
        raise typer.Exit(refuse(exc, prefix=f"{named}replay failed")) from exc
    for entry in oracle.trace():
        typer.echo(_trace_line(entry))
    for line in _rejection_lines(records, replay.decisions):
        typer.echo(line)


def _rejection_lines(records: list[dict[str, Any]], decisions: "DecisionIndex") -> list[str]:
    """The narration beside a period's trace: one line per engine-made input the period rejected
    (a stale completion, a refused routing observation, a quarantined input) and one per
    quarantine (`engine_apply_faulted`, concurrency-model ss4) whatever its source, since replay
    quarantines an undecided control input too.

    Read from the decisions replay holds, the recovered ones included, so the journal says what a
    resume would write. Unprefixed, like the trace lines; nothing replayed reads it."""
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


def _period_catalog(
    root: Path, opening: dict[str, Any], supplied: "CatalogIR | None", *, where: str
) -> CatalogIR:
    """This period's catalog: the caller's files when given, else the estate's own bundle, in
    BOTH cases checked against the hash the `segment` itself pins (period-model ss1.1), never the
    one this build writes.

    The two mismatches keep two texts: a supplied catalog that disagrees is a checkout at the
    wrong revision, and a BUNDLE that disagrees is the stored bytes failing to rebuild what they
    ran as (corruption, or a lowering that moved under a pin that did not).

    A bundle is loaded with `permit_unknown`, not from the CLI flag: these are the bytes this
    period RAN, the gate ran once at launch, and re-asking would make `dsl41 journal` refuse a
    root that `dsl41 runs` answers about (as `load_catalog_from_manifest`). The flag governs only
    the files a caller supplies."""
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


def _period_carry(root: Path, opening: dict[str, Any], *, where: str) -> "CarriedRows | None":
    """The rows this period OPENED with, or None for period 1.

    `attest.carried_from_opening` derives that fact and `open_from_seal` opens; this adds the ss11
    proofs a reader owes before trusting a sidecar it did not write. A period replayed from an
    EMPTY oracle derives revisions and run numbers the log never recorded and refuses at the first
    input touching a carried entity (DL-136), which is why a later segment named ALONE works
    (DL-141)."""
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
        help="Run roots (the --run-root of 'dsl41 run'), or the lineage"
        " anchor directory alone to read every root in the registry.",
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
    spool: when it started, how long it took, and how it ended. Rows
    sort by job and start time across all the roots given. Where the
    catalog changed between runs, the table marks the break instead of
    blending the series.

    Given the lineage anchor directory, the roots come from the
    registry, in period order. A root the registry names but the disk
    lacks is refused.

    This command reads files only. It needs no running engine and no
    socket.

    Exit codes: 0 history printed; 2 a root could not be read, or the
    registry names a root the disk lacks.
    """
    # Design: DL-113, ss1.3, PR-02f; not a control-protocol verb, which stays
    # frozen at v3
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
        # NAMED, never dropped: an archived period contributes no rows, and a quietly shorter
        # table would be silent loss (DL-144)
        typer.echo(f"warning: {line}", err=True)

    if any(row.fidelity == "records_only" for row in rows):
        # Loud on stderr as well as on the row, because the one degraded field that MISLEADS
        # rather than omits is `status`.
        typer.echo(
            "warning: some run roots have no stored inputs, so their rows are"
            " fidelity=records_only: no box rows, no box_name/started_by/job_hash,"
            " and a run closed by KILLJOB or term_run_time reads as RUNNING",
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
