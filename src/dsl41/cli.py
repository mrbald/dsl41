"""Typer CLI entry points (pyproject: `dsl41 = dsl41.cli:app`).

Exit-code contract (shared by all catalog-consuming commands): 0 success
(for lint: clean); 1 linter findings at or above the failing severity
(errors, or warnings too with --strict); 2 the input never reached the
tool (unreadable file, JIL parse error, placeholder-resolution failure,
or lowering refusal).

Templated estates (DL-19/DL-22): every catalog-consuming command accepts
--properties/-p to resolve `~{$NAME}~` placeholders before parsing, so a
bunch of templated JILs lints/reports/derives as one catalog in one step.
Substitution is within-line, so diagnostics keep pointing at the real
file and line. The typed lanes (start_times etc.) stay strict on
unresolved tokens by design -- preprocessing IS the supported path.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, cast

import typer

from dsl41.ast_jil import JilFile, JilParseError, parse, parse_file
from dsl41.ir import CatalogIR, LoweringError, lower_catalog
from dsl41.lint import lint_catalog
from dsl41.placeholders import PlaceholderError, load_properties, substitute

if TYPE_CHECKING:  # type-only: equiv's runtime import stays deferred (below)
    from datetime import datetime

    from dsl41.equiv import TierAResult, TierBCatalogResult, TierCResult

app = typer.Typer(
    no_args_is_help=True,
    help="dsl41: AutoSys->Stonebranch migration compiler.",
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """dsl41: AutoSys->Stonebranch migration compiler.

    (Callback exists only to keep typer in subcommand mode -- without it,
    typer collapses a single @app.command() into a bare top-level command
    instead of a `dsl41 <verb> ...` subcommand.)
    """


def _load_catalog_or_exit_2(
    files: Iterable[Path],
    permit_unknown: bool,
    properties: list[Path] | None = None,
) -> CatalogIR:
    try:
        parsed: list[JilFile] = []
        if properties:
            bindings = load_properties(properties)
            for path in files:
                text = path.read_bytes().decode("utf-8")
                resolved, _ = substitute(text, bindings, file=str(path))
                parsed.append(parse(resolved, file=str(path)))
        else:
            parsed = [parse_file(path) for path in files]
        return lower_catalog(parsed, permit_unknown=permit_unknown)
    except (JilParseError, LoweringError, PlaceholderError, OSError, UnicodeDecodeError) as exc:
        # OSError/UnicodeDecodeError: unreadable input (missing file, directory,
        # non-UTF-8) never reached the tool -- same exit-2 class as a refusal.
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


_PERMIT_UNKNOWN = typer.Option(
    False,
    "--permit-unknown",
    help="Carry unknown attributes verbatim instead of refusing (DL-07 escape hatch).",
)

_PROPERTIES = typer.Option(
    None,
    "--properties",
    "-p",
    help="Resolve ~{$NAME}~ placeholders from these properties file(s) before parsing"
    " (repeatable; later files override earlier, DL-19/DL-22).",
)


@app.command()
def lint(
    files: list[Path] = typer.Argument(
        ..., help="JIL files / autocal calendar exports forming one catalog"
    ),
    strict: bool = typer.Option(False, "--strict", help="Warnings also fail the exit code."),
    permit_unknown: bool = _PERMIT_UNKNOWN,
    properties: list[Path] = _PROPERTIES,
    suppress: list[str] = typer.Option(
        [],
        "--suppress",
        help="Rule code(s) to drop from the report and the exit code, e.g."
        " --suppress L005 (repeatable; comma lists accepted; DL-23).",
    ),
) -> None:
    """Parse + lower FILES into one catalog, then run the linter rules."""
    from dsl41.lint import RULE_CODES

    codes = {
        code.strip().upper() for value in suppress for code in value.split(",") if code.strip()
    }
    unknown = sorted(codes - RULE_CODES)
    if unknown:
        typer.echo(
            f"--suppress: unknown rule code(s) {', '.join(unknown)}"
            f" (known: {', '.join(sorted(RULE_CODES))})",
            err=True,
        )
        raise typer.Exit(2)
    catalog = _load_catalog_or_exit_2(files, permit_unknown, properties)
    report = lint_catalog(catalog).suppress(codes)
    for violation in report.violations:
        typer.echo(violation.render())
    raise typer.Exit(report.exit_code(strict=strict))


def _print_tier_a(result: TierAResult) -> bool:
    """Print tier-a findings; return whether it diverged."""
    typer.echo(f"tier a: {'equivalent' if result.equivalent else 'DIVERGENT'}")
    for name in result.left_only:
        typer.echo(f"  only in A: {name}")
    for name in result.right_only:
        typer.echo(f"  only in B: {name}")
    for name in result.differing:
        typer.echo(f"  {name}: {result.detail[name]}")
    return not result.equivalent


def _print_tier_b(result: TierBCatalogResult) -> bool:
    """Print tier-b findings; return whether it diverged."""
    verdict_b = "equivalent" if result.equivalent else "DIVERGENT"
    if result.equivalent and result.too_large_jobs:
        verdict_b = "equivalent where decidable"
    typer.echo(f"tier b: {verdict_b}")
    for name, why in result.divergent_jobs.items():
        typer.echo(f"  {name}: {why}")
    for name in result.too_large_jobs:
        typer.echo(f"  {name}: state space too large -- inconclusive, tier c only")
    if not result.graph_equal and result.graph_detail:
        typer.echo(f"  graph: {result.graph_detail}")
    return not result.equivalent  # too_large defers, never fails


def _print_tier_c(result: TierCResult) -> bool:
    """Print tier-c findings; return whether it diverged."""
    verdict = "equivalent" if result.equivalent else "DIVERGENT"
    typer.echo(f"tier c: {verdict} ({result.scripts_run} scripts)")
    if result.first_divergence:
        typer.echo(f"  {result.first_divergence}")
    return not result.equivalent


@app.command()
def equiv(
    files: list[Path] = typer.Argument(..., help="JIL files of catalog A"),
    against: list[Path] = typer.Option(
        ..., "--against", "-b", help="JIL files of catalog B (repeatable)."
    ),
    tier: str = typer.Option("all", "--tier", help="Which tier(s) to run: a, b, c, or all."),
    rename: list[str] = typer.Option(
        [], "--rename", help="OLD=NEW job-name mapping A->B (repeatable)."
    ),
    case_fold: bool = typer.Option(
        False, "--case-fold", help="Compare job names case-insensitively (ir-design ss6)."
    ),
    scripts: int = typer.Option(
        20, "--scripts", help="Tier-c event scripts to generate (seeded, deterministic)."
    ),
    permit_unknown: bool = _PERMIT_UNKNOWN,
    properties: list[Path] = _PROPERTIES,
) -> None:
    """Check FILES (catalog A) equivalent to --against (catalog B).

    --properties applies the same bindings to BOTH catalogs (one
    environment, two estates).

    Exit 0 when every requested tier reports equivalence, 1 on divergence,
    2 when either input never reached the comparison.
    """
    from dsl41.equiv import (
        RenameError,
        catalog_hash,
        equiv_scripts,
        equivalent_tier_a,
        equivalent_tier_b,
        equivalent_tier_c,
    )

    if tier not in ("a", "b", "c", "all"):
        typer.echo(f"--tier must be a, b, c, or all, got {tier!r}", err=True)
        raise typer.Exit(2)
    rename_map: dict[str, str] = {}
    for pair in rename:
        old, sep, new = pair.partition("=")
        if not sep or not old or not new:
            typer.echo(f"--rename expects OLD=NEW, got {pair!r}", err=True)
            raise typer.Exit(2)
        rename_map[old] = new
    catalog_a = _load_catalog_or_exit_2(files, permit_unknown, properties)
    catalog_b = _load_catalog_or_exit_2(against, permit_unknown, properties)
    try:
        if not rename_map and not case_fold and catalog_hash(catalog_a) == catalog_hash(catalog_b):
            typer.echo(
                "equivalent (canonical hashes match; ir-design ss8 short-circuit --"
                " annotations are outside the hash, ss6 softer tier)"
            )
            raise typer.Exit(0)
        divergent = False
        if tier in ("a", "all"):
            divergent |= _print_tier_a(
                equivalent_tier_a(catalog_a, catalog_b, rename=rename_map, case_fold=case_fold)
            )
        if tier in ("b", "all"):
            divergent |= _print_tier_b(
                equivalent_tier_b(catalog_a, catalog_b, rename=rename_map, case_fold=case_fold)
            )
        if tier in ("c", "all"):
            divergent |= _print_tier_c(
                equivalent_tier_c(
                    catalog_a,
                    catalog_b,
                    equiv_scripts(catalog_a, scripts=scripts),
                    rename=rename_map,
                    case_fold=case_fold,
                )
            )
    except RenameError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    raise typer.Exit(1 if divergent else 0)


@app.command()
def report(
    files: list[Path] = typer.Argument(
        ..., help="JIL files / autocal calendar exports forming one catalog"
    ),
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the markdown report here instead of stdout."
    ),
    permit_unknown: bool = _PERMIT_UNKNOWN,
    properties: list[Path] = _PROPERTIES,
) -> None:
    """Emit the per-catalog migration report (markdown).

    Always exits 0 once the report is generated -- the report IS the loud
    channel for refused/assumed constructs; use `dsl41 lint --strict` as the
    gate. Exit 2 when the input never reached the backend.
    """
    from dsl41.backend_uc import render_migration_report

    catalog = _load_catalog_or_exit_2(files, permit_unknown, properties)
    markdown = render_migration_report(catalog)
    if out is None:
        typer.echo(markdown, nl=False)
    else:
        out.write_text(markdown, encoding="utf-8")
        typer.echo(f"wrote {out}")


@app.command()
def decompile(
    files: list[Path] = typer.Argument(
        ..., help="JIL files / autocal calendar exports forming one catalog"
    ),
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the Python module here instead of stdout."
    ),
    check: bool = typer.Option(
        True,
        "--check/--no-check",
        help="Execute the emitted module and verify the rebuilt catalog's canonical"
        " hash equals the source's; divergence still emits the module but exits 1.",
    ),
    no_fold: list[str] = typer.Option(
        [],
        "--no-fold",
        help="Fold code(s) to disable (DL-38 closed set; `dsl41 folds` lists"
        " them); repeatable, comma-separated values accepted,"
        " e.g. '--no-fold T-005 --no-fold T-007' or '--no-fold T-005,T-007'.",
    ),
    permit_unknown: bool = _PERMIT_UNKNOWN,
    properties: list[Path] = _PROPERTIES,
) -> None:
    """Emit the catalog as a runnable dsl41 builder module (phase-10 DSL).

    Executing the emitted module rebuilds a catalog whose canonical form
    equals this one; --check (default on) proves that on THIS catalog before
    you rely on it -- a failure is a decompiler gap, worth a bug report, and
    exits 1 (the module is still emitted for inspection). Exit 2 when the
    input never reached the decompiler. The fold inventory and the
    stays-explicit diagnostics go to stderr.
    """
    from dsl41.dsl import DslError
    from dsl41.dsl import decompile as decompile_catalog

    catalog = _load_catalog_or_exit_2(files, permit_unknown, properties)
    fold_report: list[str] = []
    try:
        source = decompile_catalog(
            catalog,
            disable=[code for chunk in no_fold for code in chunk.split(",")],
            report=fold_report,
        )
    except DslError as exc:
        # a decompiler refusal (nothing emittable, unknown fold code) is the
        # same class as a lowering refusal: the input never became output
        # (DL-37a)
        typer.echo(f"decompile refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    # Emit BEFORE checking (DL-37a): the module must survive for inspection
    # even when the check finds a decompiler gap.
    if out is None:
        typer.echo(source, nl=False)
    else:
        out.write_text(source, encoding="utf-8")
        typer.echo(f"wrote {out}")
    for line in fold_report:
        typer.echo(f"fold: {line}", err=True)
    if check:
        from dsl41.equiv import catalog_hash, equivalent_tier_a

        namespace: dict[str, object] = {"__name__": "<decompiled>"}
        try:
            exec(compile(source, "<decompiled>", "exec"), namespace)  # noqa: S102
        except Exception as exc:
            typer.echo(
                "round-trip check FAILED (a decompiler gap, not your input):"
                f" the emitted module raised {type(exc).__name__}: {exc}",
                err=True,
            )
            raise typer.Exit(1) from exc
        rebuilt = namespace["catalog"]
        assert isinstance(rebuilt, CatalogIR)
        if catalog_hash(rebuilt) != catalog_hash(catalog):
            result = equivalent_tier_a(catalog, rebuilt)
            divergence = "; ".join(f"{k}: {v}" for k, v in sorted(result.detail.items())) or (
                "hash mismatch with no tier-a detail -- report this with the input"
            )
            typer.echo(
                f"round-trip check FAILED (a decompiler gap, not your input): {divergence}",
                err=True,
            )
            raise typer.Exit(1)


@app.command()
def journal(
    journal_file: Path = typer.Argument(
        ..., help="Run journal to replay (<run_root>/journal.jsonl)"
    ),
    files: list[Path] = typer.Argument(..., help="JIL files forming the catalog the run used"),
    permit_unknown: bool = _PERMIT_UNKNOWN,
    properties: list[Path] = _PROPERTIES,
) -> None:
    """Replay a run journal's inputs through a fresh Oracle and print the
    reconstructed trace.

    The WAL is inputs-only (runner-design ss7): emitted events and the trace
    are pure functions of the input sequence, so they are derived here, never
    stored. Refuses on catalog-hash mismatch -- a changed estate re-baselines
    explicitly.
    """
    from dsl41.oracle import Oracle, OracleError
    from dsl41.runner import EngineError, catalog_hash, read_journal, replay_inputs

    catalog = _load_catalog_or_exit_2(files, permit_unknown, properties)
    try:
        records = read_journal(journal_file)
    except (OSError, EngineError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    header = records[0]
    if header.get("catalog_hash") != catalog_hash(catalog):
        typer.echo(
            "catalog hash mismatch: the estate differs from the one this journal ran"
            " (runner-design ss7: no silent semantic drift)",
            err=True,
        )
        raise typer.Exit(2)
    oracle = Oracle(catalog)
    try:
        replay_inputs(oracle, records)
    except OracleError as exc:
        typer.echo(f"replay failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    for entry in oracle.trace():
        typer.echo(f"{entry.at.isoformat()} {entry.job} {entry.transition} [{entry.cause}]")


@app.command()
def folds() -> None:
    """List the decompiler's built-in fold registry (DL-38 closed set)."""
    from dsl41.dsl import FOLDS

    for code, description in FOLDS.items():
        typer.echo(f"{code}  {description}")


@app.command()
def resolve(
    files: list[Path] = typer.Argument(
        ..., help="Templated JIL (or any text) file(s); several files merge in order."
    ),
    properties: list[Path] = typer.Option(
        ...,
        "--properties",
        "-p",
        help="Properties file(s) with KEY=VALUE lines; later files override earlier (repeatable).",
    ),
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the resolved text here instead of stdout."
    ),
    permit_unresolved: bool = typer.Option(
        False,
        "--permit-unresolved",
        help="Leave unresolved/malformed ~{...}~ tokens verbatim (reported on stderr)"
        " instead of failing.",
    ),
) -> None:
    """Resolve estate `~{$NAME}~` placeholders in FILES from properties files.

    Non-core preprocessor (DL-19/DL-22): reproduces the estate templating
    step so resolved JIL flows through the ordinary pipeline. Several FILES
    concatenate in argument order into one output (a missing final newline
    between inputs is completed in that input's own style; merging LF and
    CRLF inputs is refused -- statement-syntax rule 10 makes the merged
    text unparseable). Exit 0 on success (including permitted leftovers,
    which are reported on stderr); exit 2 when the properties or any input
    cannot be resolved.
    """
    try:
        bindings = load_properties(properties)
        chunks: list[str] = []
        reports: list[str] = []
        for path in files:
            text = path.read_bytes().decode("utf-8")
            resolved, file_reports = substitute(
                text, bindings, file=str(path), permit_unresolved=permit_unresolved
            )
            chunks.append(resolved)
            reports.extend(file_reports)
        if len({"\r\n" if "\r\n" in chunk else "\n" for chunk in chunks if chunk}) > 1:
            raise PlaceholderError(
                [
                    "merging these inputs would mix LF and CRLF line endings"
                    " (statement-syntax rule 10); normalize them first"
                ]
            )
    except (PlaceholderError, OSError, UnicodeDecodeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    for report in reports:
        typer.echo(report, err=True)
    merged = ""
    for chunk in chunks:
        if merged and not merged.endswith("\n"):
            merged += "\r\n" if "\r\n" in merged else "\n"
        merged += chunk
    if out is None:
        typer.echo(merged, nl=False)
    else:
        out.write_bytes(merged.encode("utf-8"))  # bytes: keep line endings exact
        typer.echo(f"wrote {out}")


@app.command()
def viz(
    files: list[Path] = typer.Argument(
        ..., help="JIL files / autocal calendar exports forming one catalog"
    ),
    collapse_threshold: int = typer.Option(
        None,
        "--collapse-threshold",
        help="Boxes with more direct members than this render as one node.",
        show_default="12",
    ),
    direction: str = typer.Option(
        "auto",
        "--direction",
        help="Chart direction: auto (per-component heuristic), LR, or TD.",
    ),
    include_singletons: bool = typer.Option(
        False,
        "--include-singletons",
        help="Also chart standalone jobs (they are always listed in Appendix A).",
    ),
    elk: bool = typer.Option(
        False,
        "--elk",
        help="Prepend Mermaid ELK-layout frontmatter (VS Code/local; GitHub ignores it).",
    ),
    out: Path = typer.Option(None, "--out", "-o", help="Write the report here, not stdout."),
    permit_unknown: bool = _PERMIT_UNKNOWN,
    properties: list[Path] = _PROPERTIES,
) -> None:
    """Render FILES' derived dependency graph as a Markdown report of
    per-workflow Mermaid charts (DL-35)."""
    from dsl41.derive import derive_graph
    from dsl41.viz import DEFAULT_COLLAPSE_THRESHOLD, to_markdown

    if direction not in ("auto", "LR", "TD"):
        typer.echo(f"--direction must be auto, LR, or TD, got {direction!r}", err=True)
        raise typer.Exit(2)
    catalog = _load_catalog_or_exit_2(files, permit_unknown, properties)
    threshold = DEFAULT_COLLAPSE_THRESHOLD if collapse_threshold is None else collapse_threshold
    report = to_markdown(
        derive_graph(catalog),
        title=", ".join(f.name for f in files),
        collapse_threshold=threshold,
        direction=direction,  # type: ignore[arg-type]  # validated above
        include_singletons=include_singletons,
        elk=elk,
    )
    if out is None:
        typer.echo(report, nl=False)
    else:
        out.write_bytes(report.encode("utf-8"))  # bytes: keep line endings exact
        typer.echo(f"wrote {out}")


# ------------------------------------------------------------------- runner (phase 11)
#
# Exit codes for the runner verbs: 0 clean (run: operator-stopped; rehearse:
# quiescent; sendevent/query: ok response), 1 the engine/estate failed while
# running (EngineError, oracle refusal mid-run), 2 the run never started
# (preflight ERROR, resume gate, unreadable scenario, unreachable socket).


def _preflight_or_exit(
    catalog: CatalogIR,
    *,
    execution: bool,
    machine_policy: str = "strict",
    as_machine: "list[str] | None" = None,
) -> list:
    """Print ss8 findings; exit 2 on any ERROR; return the WARNs (the caller
    journals them next to the run -- WARN prints, journals, and runs)."""
    from dsl41.runner import MachinePolicy, preflight

    if machine_policy not in ("strict", "local-eligible"):
        typer.echo(f"--machine-policy {machine_policy!r}: expected strict|local-eligible", err=True)
        raise typer.Exit(2)
    items = preflight(
        catalog,
        execution=execution,
        machine_policy=cast("MachinePolicy", machine_policy),
        as_machine=frozenset(as_machine or ()),
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
        typer.echo(f"{option}: {exc}", err=True)
        raise typer.Exit(2) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


_TIMEZONE_OPT = typer.Option(
    None,
    "--timezone",
    help="Base zone for schedules without a per-job timezone (PENDING: E10;"
    " default UTC -- vendor uses the server's zone).",
)


def _check_base_tz(timezone: str | None) -> None:
    """Preflight the run-level base zone: per-job zones gate in ss8, but the
    --timezone flag would otherwise surface as a raw ZoneInfo traceback from
    the Scheduler with the wrong exit code (DL-45 review M3)."""
    if timezone is None:
        return
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(timezone)
    except (KeyError, ValueError, OSError) as exc:
        typer.echo(f"--timezone {timezone!r} is not resolvable in zoneinfo: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command()
def run(
    files: list[Path] = typer.Argument(..., help="JIL files forming the estate to execute"),
    run_root: Path = typer.Option(
        ..., "--run-root", help="Run directory (journal, runs/, logs/, control.sock)."
    ),
    resume: bool = typer.Option(
        False, "--resume", help="Resume the run_root's journal (replay + reconcile, ss7)."
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
    timezone: str = _TIMEZONE_OPT,
    permit_unknown: bool = _PERMIT_UNKNOWN,
    properties: list[Path] = _PROPERTIES,
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

    if ui:
        _import_tui_or_exit_2()  # fail before the engine starts, not after
    catalog = _load_catalog_or_exit_2(files, permit_unknown, properties)
    warns = _preflight_or_exit(
        catalog, execution=True, machine_policy=machine_policy, as_machine=as_machine
    )
    _check_base_tz(timezone)
    from dsl41.runner import EngineError

    try:
        raise typer.Exit(
            asyncio.run(_serve_run(catalog, run_root, resume, timezone, warns, ui, detached))
        )
    except EngineError as exc:
        # start/resume gates (existing journal, hash/domain mismatch, live
        # socket): the run never started
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


def _import_tui_or_exit_2():
    """Guarded textual import (runner-design ss14): the core package keeps
    its three runtime deps; the TUI is the optional [ui] extra."""
    try:
        from dsl41 import runner_tui
    except ModuleNotFoundError as exc:
        typer.echo("the TUI needs the optional [ui] extra: pip install 'dsl41[ui]'", err=True)
        raise typer.Exit(2) from exc
    return runner_tui


async def _serve_run(
    catalog: CatalogIR,
    run_root: Path,
    resume: bool,
    timezone: str | None,
    warns: list,
    ui: bool = False,
    detached: bool = False,
) -> int:
    import asyncio
    import contextlib
    import signal as signal_mod

    from datetime import datetime

    from dsl41.runner import (
        ControlServer,
        EngineError,
        FileWatcherAdapter,
        JobAdapter,
        LocalCommandAdapter,
        RealClock,
        Scheduler,
        SupervisedCommandAdapter,
        SupervisorClient,
        SupervisorUnavailable,
        start_run,
    )
    from dsl41.runner import resume_run as _resume_run

    clock = RealClock()
    # detached (ss6a Tier 1, spec ss3): the CMD adapter SPAWNs through a
    # supervisor that owns the wrapper lifelines, so an engine restart does
    # not kill the jobs. FW stays in-engine (no process to survive).
    client: SupervisorClient | None = None
    if detached:
        run_root.mkdir(parents=True, exist_ok=True)  # the supervisor needs it first
        client = SupervisorClient(run_root)
        try:
            await client.ensure_running()
            await client.acquire()
        except SupervisorUnavailable as exc:
            typer.echo(f"supervisor unavailable: {exc}", err=True)
            return 2
        adapters: dict[str, JobAdapter] = {
            "CMD": SupervisedCommandAdapter(client),
            "FW": FileWatcherAdapter(),
        }
    else:
        adapters = {"CMD": LocalCommandAdapter(), "FW": FileWatcherAdapter()}
    scheduler = Scheduler(catalog, start=clock.now(), default_tz=timezone)
    if resume:
        engine = await _resume_run(
            catalog,
            run_root,
            clock=clock,
            adapters=adapters,
            scheduler=scheduler,
            hold_open=True,
            supervisor=client,
        )
    else:
        engine = start_run(
            catalog, run_root, clock=clock, adapters=adapters, scheduler=scheduler, hold_open=True
        )
    for ev, reason in engine.drops:  # resume's missed-tick sweep (PENDING: E9)
        typer.echo(f"dropped {ev.kind} {ev.job() or ''} @ {ev.at.isoformat()}: {reason}", err=True)
    if warns and engine.journal is not None:
        engine.journal.preflight(warns)
    server = ControlServer(engine, run_root / "control.sock")
    try:
        await server.start()
    except EngineError as exc:
        typer.echo(str(exc), err=True)
        return 2
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
    if loop_task in done:  # hold_open never quiesces: this is a crash
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


@app.command()
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
    timezone: str = _TIMEZONE_OPT,
    run_root: Path = typer.Option(
        None, "--run-root", help="Also persist a WAL journal under this directory."
    ),
    permit_unknown: bool = _PERMIT_UNKNOWN,
    properties: list[Path] = _PROPERTIES,
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

    from dsl41.oracle import Event, OracleError
    from dsl41.runner import Engine, EngineError, FakeAdapter, Scheduler, VirtualClock, start_run

    catalog = _load_catalog_or_exit_2(files, permit_unknown, properties)
    warns = _preflight_or_exit(catalog, execution=False)
    _check_base_tz(timezone)
    start_dt = (
        _naive_utc_arg(start, "--start")
        if start
        else datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    )
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
            typer.echo(f"scenario {scenario}: {exc}", err=True)
            raise typer.Exit(2) from exc
    clock = VirtualClock(start_dt)
    adapter = FakeAdapter(script, default=default)
    scheduler = Scheduler(catalog, start=start_dt, default_tz=timezone)
    adapters = {"CMD": adapter, "FW": adapter}
    try:
        if run_root is not None:
            engine = start_run(
                catalog, run_root, clock=clock, adapters=adapters, scheduler=scheduler
            )
        else:
            engine = Engine(catalog, clock=clock, adapters=adapters, scheduler=scheduler)
    except EngineError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
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


def _control_roundtrip(socket_path: Path, request: dict) -> dict:
    import json as json_mod
    import socket as socket_mod

    try:
        conn = socket_mod.socket(socket_mod.AF_UNIX)
        conn.settimeout(10.0)
        conn.connect(str(socket_path))
        conn.sendall(json_mod.dumps(request).encode("utf-8") + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        conn.close()
        return json_mod.loads(buf)
    except (OSError, ValueError) as exc:
        typer.echo(f"control socket {socket_path}: {exc}", err=True)
        raise typer.Exit(2) from exc


_SOCKET_OPT = typer.Option(
    ...,
    "--socket",
    "-S",
    help="The engine's control socket (<run_root>/control.sock).",
)


@app.command()
def sendevent(
    event: str = typer.Argument(
        ...,
        help="STARTJOB|FORCE_STARTJOB|KILLJOB|ON_ICE|OFF_ICE|ON_HOLD|OFF_HOLD"
        "|ON_NOEXEC|OFF_NOEXEC|SET_GLOBAL|CHANGE_STATUS",
    ),
    socket_path: Path = _SOCKET_OPT,
    job: str = typer.Option(None, "--job", "-J", help="Target job (job verbs, CHANGE_STATUS)."),
    status: str = typer.Option(None, "--status", "-s", help="CHANGE_STATUS: the new status."),
    global_kv: str = typer.Option(None, "--global", "-G", help='SET_GLOBAL: "NAME=value".'),
    exit_code: int = typer.Option(
        None, "--exit-code", help="CHANGE_STATUS: optional exit code to record."
    ),
) -> None:
    """Vendor-parity sendevent against a running engine (runner-design ss10).
    Every accepted event is journaled by the engine (source=control)."""
    import json as json_mod

    request: dict = {"cmd": "sendevent", "event": event.upper()}
    if job is not None:
        request["job"] = job
    if status is not None:
        request["status"] = status.upper()
    if global_kv is not None:
        name, sep, value = global_kv.partition("=")
        if not sep or not name:
            typer.echo('--global expects "NAME=value"', err=True)
            raise typer.Exit(2)
        request["name"], request["value"] = name, value
    if exit_code is not None:
        request["exit_code"] = exit_code
    response = _control_roundtrip(socket_path, request)
    typer.echo(json_mod.dumps(response, sort_keys=True))
    raise typer.Exit(0 if response.get("ok") else 2)


@app.command()
def ui(socket_path: Path = _SOCKET_OPT) -> None:
    """Attach the ss11 Textual TUI to a running engine: jobs table, explain
    pane with per-atom truth, log tail, sendevent console. A thin client of
    the control socket only -- quitting detaches the viewer and leaves the
    run alone (unlike `run --ui`, whose terminal owns the run)."""
    runner_tui = _import_tui_or_exit_2()
    if not socket_path.exists():
        typer.echo(f"control socket {socket_path}: no such file", err=True)
        raise typer.Exit(2)
    runner_tui.RunnerApp(socket_path).run()


def _import_textual_serve_or_exit_2():
    """Guarded textual-serve import (runner-design ss11/ss14): the [ui]
    extra's other half -- textual-serve spawns one app subprocess per
    browser session, so it needs its own dependency, not just textual's."""
    try:
        from textual_serve.server import Server
    except ModuleNotFoundError as exc:
        typer.echo("`serve` needs the optional [ui] extra: pip install 'dsl41[ui]'", err=True)
        raise typer.Exit(2) from exc
    return Server


@app.command()
def serve(
    socket_path: Path = _SOCKET_OPT,
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address (loopback default: textual-serve ships no"
        " auth, ss11 -- put a proxy or tunnel in front for remote access).",
    ),
    port: int = typer.Option(8000, "--port", help="Bind port."),
) -> None:
    """Serve the ss11 TUI over the web via textual-serve: one app subprocess
    per browser session, each `dsl41 ui --socket` against this same running
    engine -- never in-process with the engine, so no viewer gets a private
    universe (ss11). No auth of its own; see README's deployment notes
    before exposing this beyond loopback."""
    import shlex
    import sys

    server_cls = _import_textual_serve_or_exit_2()
    if not socket_path.exists():
        typer.echo(f"control socket {socket_path}: no such file", err=True)
        raise typer.Exit(2)
    command = f"{shlex.quote(sys.executable)} -m dsl41 ui --socket {shlex.quote(str(socket_path))}"
    try:
        server_cls(command, host=host, port=port).serve()
    except OSError as exc:
        typer.echo(f"serve {host}:{port}: {exc}", err=True)
        raise typer.Exit(2) from exc


@app.command()
def query(
    what: str = typer.Argument(..., help="status|trace|explain|plan|subscribe"),
    socket_path: Path = _SOCKET_OPT,
    job: str = typer.Option(None, "--job", "-J", help="status: filter; explain: the job."),
    since: int = typer.Option(None, "--since", help="trace/subscribe: only records after SEQ."),
) -> None:
    """Read-only control-plane queries (runner-design ss10); `subscribe`
    streams journal records as JSON lines until interrupted. The headless
    autorep analog -- the ss11 TUI consumes the same verbs."""
    import json as json_mod
    import socket as socket_mod

    verb = what.lower()
    if verb not in ("status", "trace", "explain", "plan", "subscribe"):
        typer.echo(f"unknown query {what!r} (status|trace|explain|plan|subscribe)", err=True)
        raise typer.Exit(2)
    request: dict = {"cmd": verb}
    if job is not None:
        request["job"] = job
    if since is not None:
        request["since"] = since
    if verb != "subscribe":
        response = _control_roundtrip(socket_path, request)
        typer.echo(json_mod.dumps(response, indent=2, sort_keys=True))
        raise typer.Exit(0 if response.get("ok") else 2)
    try:
        conn = socket_mod.socket(socket_mod.AF_UNIX)
        conn.connect(str(socket_path))
        conn.sendall(json_mod.dumps(request).encode("utf-8") + b"\n")
        with conn.makefile("rb") as stream:
            for line in stream:
                typer.echo(line.decode("utf-8").rstrip("\n"))
    except OSError as exc:
        typer.echo(f"control socket {socket_path}: {exc}", err=True)
        raise typer.Exit(2) from exc
    except KeyboardInterrupt:
        pass


class _SupervisorConn:
    """One persistent connection to a supervisor socket, request/response with
    async exit PUSHes skipped (the CLI is not a data-channel consumer)."""

    def __init__(self, sock_path: Path) -> None:
        import socket as socket_mod

        self.conn = socket_mod.socket(socket_mod.AF_UNIX)
        # SHUTDOWN replies only AFTER waiting for wrappers (frozen ss5 order),
        # which spans the spawn-record wait plus per-run grace windows
        self.conn.settimeout(60.0)
        self.conn.connect(str(sock_path))
        self.buf = b""

    def send(self, request: dict) -> dict:
        import json as json_mod

        self.conn.sendall(json_mod.dumps({**request, "v": 1}).encode("utf-8") + b"\n")
        while True:
            while b"\n" not in self.buf:
                chunk = self.conn.recv(65536)
                if not chunk:
                    raise OSError("supervisor closed the connection")
                self.buf += chunk
            line, self.buf = self.buf.split(b"\n", 1)
            obj = json_mod.loads(line)
            if isinstance(obj, dict) and obj.get("push"):
                continue  # notifications are droppable (supervisor-protocol ss5)
            return obj

    def close(self) -> None:
        self.conn.close()


@app.command()
def supervise(
    action: str = typer.Argument(..., help="list|shutdown"),
    run_root: Path = typer.Option(
        ..., "--run-root", help="Run directory holding supervisor.sock (ss6a Tier 1)."
    ),
) -> None:
    """Observe or stop a run-root's supervisor (runner-design ss6a; DL-42 item
    4 -- read-only by default). `list` prints its live runs and lease; `shutdown`
    ACQUIREs the lease (failing loudly with holder info while an engine holds an
    unexpired one), then SHUTDOWNs: TERM->grace->KILL each command, wrappers
    record truthfully, socket + pidfile removed. Exit 2 when there is no
    supervisor or the lease could not be taken; 0 on a clean shutdown."""
    import json as json_mod
    import os

    verb = action.lower()
    if verb not in ("list", "shutdown"):
        typer.echo(f"unknown supervise action {action!r} (list|shutdown)", err=True)
        raise typer.Exit(2)
    sock_path = run_root / "supervisor.sock"
    if not sock_path.exists():
        typer.echo(f"no supervisor at {sock_path}", err=True)
        raise typer.Exit(2)
    try:
        conn = _SupervisorConn(sock_path)
    except OSError as exc:
        typer.echo(f"supervisor {sock_path}: {exc}", err=True)
        raise typer.Exit(2) from exc
    try:
        if verb == "list":
            resp = conn.send({"cmd": "LIST"})
            typer.echo(json_mod.dumps(resp, indent=2, sort_keys=True))
            raise typer.Exit(0 if resp.get("ok") else 2)
        acq = conn.send(
            {"cmd": "ACQUIRE", "controller_id": f"supervise-cli-{os.getpid()}", "ttl_s": 60}
        )
        if not acq.get("ok"):
            typer.echo(f"cannot acquire lease: {json_mod.dumps(acq, sort_keys=True)}", err=True)
            raise typer.Exit(2)
        resp = conn.send({"cmd": "SHUTDOWN", "token": acq["token"]})
        typer.echo(json_mod.dumps(resp, sort_keys=True))
        raise typer.Exit(0 if resp.get("ok") else 2)
    except OSError as exc:
        typer.echo(f"supervisor {sock_path}: {exc}", err=True)
        raise typer.Exit(2) from exc
    finally:
        conn.close()
