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
from typing import TYPE_CHECKING

import typer

from dsl41.ast_jil import JilFile, JilParseError, parse, parse_file
from dsl41.ir import CatalogIR, LoweringError, lower_catalog
from dsl41.lint import lint_catalog
from dsl41.placeholders import PlaceholderError, load_properties, substitute

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
    permit_unknown: bool = _PERMIT_UNKNOWN,
    properties: list[Path] = _PROPERTIES,
) -> None:
    """Emit the catalog as a runnable dsl41 builder module (phase-10 DSL).

    Executing the emitted module rebuilds a catalog whose canonical form
    equals this one; --check (default on) proves that on THIS catalog before
    you rely on it -- a failure is a decompiler gap, worth a bug report, and
    exits 1 (the module is still emitted for inspection). Exit 2 when the
    input never reached the decompiler.
    """
    from dsl41.dsl import decompile as decompile_catalog

    catalog = _load_catalog_or_exit_2(files, permit_unknown, properties)
    source = decompile_catalog(catalog)
    divergence: str | None = None
    if check:
        from dsl41.equiv import catalog_hash, equivalent_tier_a

        namespace: dict[str, object] = {"__name__": "<decompiled>"}
        exec(compile(source, "<decompiled>", "exec"), namespace)  # noqa: S102
        rebuilt = namespace["catalog"]
        assert isinstance(rebuilt, CatalogIR)
        if catalog_hash(rebuilt) != catalog_hash(catalog):
            result = equivalent_tier_a(catalog, rebuilt)
            divergence = "; ".join(f"{k}: {v}" for k, v in sorted(result.detail.items())) or (
                "hash mismatch with no tier-a detail (softer-tier fields, e.g. annotations)"
            )
    if out is None:
        typer.echo(source, nl=False)
    else:
        out.write_text(source, encoding="utf-8")
        typer.echo(f"wrote {out}")
    if divergence is not None:
        typer.echo(
            f"round-trip check FAILED (a decompiler gap, not your input): {divergence}",
            err=True,
        )
        raise typer.Exit(1)


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
