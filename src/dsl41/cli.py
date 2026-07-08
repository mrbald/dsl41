"""Typer CLI entry points (pyproject: `dsl41 = dsl41.cli:app`).

Exit-code contract (shared by all catalog-consuming commands): 0 success
(for lint: clean); 1 linter findings at or above the failing severity
(errors, or warnings too with --strict); 2 the input never reached the
tool (unreadable file, JIL parse error, or lowering refusal).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from dsl41.ast_jil import JilParseError, parse_file
from dsl41.ir import CatalogIR, LoweringError, lower_catalog
from dsl41.lint import lint_catalog

if TYPE_CHECKING:  # type-only: equiv's runtime import stays deferred (below)
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


def _load_catalog_or_exit_2(files: Iterable[Path], permit_unknown: bool) -> CatalogIR:
    try:
        return lower_catalog([parse_file(path) for path in files], permit_unknown=permit_unknown)
    except (JilParseError, LoweringError, OSError, UnicodeDecodeError) as exc:
        # OSError/UnicodeDecodeError: unreadable input (missing file, directory,
        # non-UTF-8) never reached the tool -- same exit-2 class as a refusal.
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


_PERMIT_UNKNOWN = typer.Option(
    False,
    "--permit-unknown",
    help="Carry unknown attributes verbatim instead of refusing (DL-07 escape hatch).",
)


@app.command()
def lint(
    files: list[Path] = typer.Argument(..., help="JIL files forming one catalog"),
    strict: bool = typer.Option(False, "--strict", help="Warnings also fail the exit code."),
    permit_unknown: bool = _PERMIT_UNKNOWN,
) -> None:
    """Parse + lower FILES into one catalog, then run the linter rules."""
    catalog = _load_catalog_or_exit_2(files, permit_unknown)
    report = lint_catalog(catalog)
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
) -> None:
    """Check FILES (catalog A) equivalent to --against (catalog B).

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
    catalog_a = _load_catalog_or_exit_2(files, permit_unknown)
    catalog_b = _load_catalog_or_exit_2(against, permit_unknown)
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
    files: list[Path] = typer.Argument(..., help="JIL files forming one catalog"),
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the markdown report here instead of stdout."
    ),
    permit_unknown: bool = _PERMIT_UNKNOWN,
) -> None:
    """Emit the per-catalog migration report (markdown).

    Always exits 0 once the report is generated -- the report IS the loud
    channel for refused/assumed constructs; use `dsl41 lint --strict` as the
    gate. Exit 2 when the input never reached the backend.
    """
    from dsl41.backend_uc import render_migration_report

    catalog = _load_catalog_or_exit_2(files, permit_unknown)
    markdown = render_migration_report(catalog)
    if out is None:
        typer.echo(markdown, nl=False)
    else:
        out.write_text(markdown, encoding="utf-8")
        typer.echo(f"wrote {out}")


@app.command()
def decompile(
    files: list[Path] = typer.Argument(..., help="JIL files forming one catalog"),
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the Python module here instead of stdout."
    ),
    permit_unknown: bool = _PERMIT_UNKNOWN,
) -> None:
    """Emit the catalog as a runnable dsl41 builder module (phase-10 DSL).

    Executing the emitted module rebuilds a catalog whose canonical form
    equals this one. Exit 2 when the input never reached the decompiler.
    """
    from dsl41.dsl import decompile as decompile_catalog

    catalog = _load_catalog_or_exit_2(files, permit_unknown)
    source = decompile_catalog(catalog)
    if out is None:
        typer.echo(source, nl=False)
    else:
        out.write_text(source, encoding="utf-8")
        typer.echo(f"wrote {out}")


@app.command()
def viz(
    files: list[Path] = typer.Argument(..., help="JIL files forming one catalog"),
    collapse_threshold: int = typer.Option(
        None,
        "--collapse-threshold",
        help="Boxes with more direct members than this render as one node.",
        show_default="12",
    ),
    direction: str = typer.Option("LR", "--direction", help="Mermaid flow direction: LR or TD."),
    permit_unknown: bool = _PERMIT_UNKNOWN,
) -> None:
    """Render FILES' derived dependency graph as Mermaid on stdout."""
    from dsl41.derive import derive_graph
    from dsl41.viz import DEFAULT_COLLAPSE_THRESHOLD, to_mermaid

    if direction not in ("LR", "TD"):
        typer.echo(f"--direction must be LR or TD, got {direction!r}", err=True)
        raise typer.Exit(2)
    catalog = _load_catalog_or_exit_2(files, permit_unknown)
    threshold = DEFAULT_COLLAPSE_THRESHOLD if collapse_threshold is None else collapse_threshold
    mermaid = to_mermaid(
        derive_graph(catalog),
        collapse_threshold=threshold,
        direction=direction,  # type: ignore[arg-type]  # validated above
    )
    typer.echo(mermaid, nl=False)
