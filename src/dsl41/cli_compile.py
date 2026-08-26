"""The compiler verbs: a catalog in, an artifact out (DL-137's split).

`lint`, `equiv`, `report`, `uc`, `decompile`, `folds`, `resolve` and `viz`
-- every verb that reads JIL and writes a finding, a report, a bundle, a
module or a chart, and touches no run root and no socket. Registered on
the app in `cli.py`; the exit-code contract is stated there.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from dsl41.cli_common import (
    CATALOG_FILES,
    PERMIT_UNKNOWN,
    PROPERTIES,
    load_catalog_or_exit_2,
    refuse,
)
from dsl41.ir import CatalogIR
from dsl41.lint import lint_catalog
from dsl41.placeholders import PlaceholderError, load_properties, substitute

if TYPE_CHECKING:  # type-only: equiv's runtime import stays deferred (below)
    from dsl41.equiv import TierAResult, TierBCatalogResult, TierCResult


def _emit(body: str, out: "Path | None") -> None:
    """Write BODY to --out, or echo it to stdout -- one spelling of the five
    emitters `report`/`uc`/`decompile`/`resolve`/`viz` used to write by hand
    (DL-178g). Always writes bytes to the file: line endings survive exact,
    which is what the three callers that used to `write_text` needed too --
    `write_text` newline-translates on Windows, `write_bytes` never does, and
    on POSIX the two are the same bytes. The caller supplies BODY exactly as
    it should read in both places, including any trailing newline (`uc`
    wants one; the rest do not)."""
    if out is None:
        typer.echo(body, nl=False)
    else:
        out.write_bytes(body.encode("utf-8"))
        typer.echo(f"wrote {out}")


def lint(
    files: list[Path] = CATALOG_FILES,
    strict: bool = typer.Option(False, "--strict", help="Warnings also fail the exit code."),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
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
        raise typer.Exit(
            refuse(
                f"--suppress: unknown rule code(s) {', '.join(unknown)}"
                f" (known: {', '.join(sorted(RULE_CODES))})"
            )
        )
    catalog = load_catalog_or_exit_2(files, permit_unknown, properties)
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
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
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
        raise typer.Exit(refuse(f"--tier must be a, b, c, or all, got {tier!r}"))
    rename_map: dict[str, str] = {}
    for pair in rename:
        old, sep, new = pair.partition("=")
        if not sep or not old or not new:
            raise typer.Exit(refuse(f"--rename expects OLD=NEW, got {pair!r}"))
        rename_map[old] = new
    catalog_a = load_catalog_or_exit_2(files, permit_unknown, properties)
    catalog_b = load_catalog_or_exit_2(against, permit_unknown, properties)
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
            if tier == "b":
                # tier b reads set(A.jobs) & set(B.jobs) and compares edges,
                # not the node list: two disjoint catalogs can both come back
                # equivalent. Tier (a) owns the job-set question (ir-design
                # ss6), so say so rather than let "equivalent" over-read.
                typer.echo(
                    "  note: tier b compares only the jobs both catalogs define;"
                    " a job present in one catalog alone is tier (a)'s question"
                    " (ir-design ss6) -- run --tier a or --tier all to settle it"
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
        raise typer.Exit(refuse(exc)) from exc
    raise typer.Exit(1 if divergent else 0)


def report(
    files: list[Path] = CATALOG_FILES,
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the markdown report here instead of stdout."
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Emit the per-catalog migration report (markdown).

    Always exits 0 once the report is generated -- the report IS the loud
    channel for refused/assumed constructs; use `dsl41 lint --strict` as the
    gate. Exit 2 when the input never reached the backend.
    """
    from dsl41.backend_uc import render_migration_report

    catalog = load_catalog_or_exit_2(files, permit_unknown, properties)
    markdown = render_migration_report(catalog)
    _emit(markdown, out)


def uc(
    files: list[Path] = CATALOG_FILES,
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the JSON bundle here instead of stdout."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit 1 when any workflow was quarantined."
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Emit the U3a base CREATE-ONLY UC workflow record bundle (JSON).

    One taskWorkflow record per serializable workflow, exactly the shape
    frozen in docs/uc-edge-schema.md. A workflow is QUARANTINED whole for
    either of two causes -- an edge the base schema cannot express, or a
    record name a second workflow also serializes to -- and every one is
    listed in the bundle's own ledger (summarized on stderr). Exit 0 once a
    bundle is generated (1 with --strict when anything was quarantined);
    exit 2 when the input never reached the backend.
    """
    from dsl41.backend_uc import compile_to_uc

    catalog = load_catalog_or_exit_2(files, permit_unknown, properties)
    bundle = compile_to_uc(catalog)
    # +"\n": the one emitter that wants its body to end in one, in both
    # places (`_emit` writes and echoes BODY as given, never adding its own)
    _emit(bundle.model_dump_json(indent=2) + "\n", out)
    typer.echo(
        f"{len(bundle.records)} record(s); {len(bundle.quarantined)} quarantined",
        err=True,
    )
    for workflow in bundle.quarantined:
        for reason in workflow.reasons:
            typer.echo(f"quarantined {workflow.name}: {reason}", err=True)
    if strict and bundle.quarantined:
        raise typer.Exit(1)


def decompile(
    files: list[Path] = CATALOG_FILES,
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
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
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

    catalog = load_catalog_or_exit_2(files, permit_unknown, properties)
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
        raise typer.Exit(refuse(exc, prefix="decompile refused")) from exc
    # Emit BEFORE checking (DL-37a): the module must survive for inspection
    # even when the check finds a decompiler gap.
    _emit(source, out)
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


def folds() -> None:
    """List the decompiler's built-in fold registry (DL-38 closed set)."""
    from dsl41.dsl import FOLDS

    for code, description in FOLDS.items():
        typer.echo(f"{code}  {description}")


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
        raise typer.Exit(refuse(exc)) from exc
    for report in reports:
        typer.echo(report, err=True)
    merged = ""
    for chunk in chunks:
        if merged and not merged.endswith("\n"):
            merged += "\r\n" if "\r\n" in merged else "\n"
        merged += chunk
    _emit(merged, out)


class VizFormat(str, Enum):
    """The five viz outputs, exclusive by construction (DL-75). They used to
    be three booleans -- eight combinations for five modes, plus a precedence
    rule (--explore beat --html) and per-flag prose about what each nullified.
    html-chart is the mode DL-75 miscounted away and DL-76 brought back; it
    returns as a value here, never as a flag combination."""

    report = "report"
    chart = "chart"
    html = "html"
    html_chart = "html-chart"
    explore = "explore"


def _refuse_removed_viz_flags(whole_graph: bool, html: bool, explore: bool) -> None:
    """DL-75: the three mode booleans are gone. Naming the replacement beats
    a bare "no such option" for anyone with the old command in a script --
    and the one COMBINATION that named a mode of its own (DL-70(4)'s
    --html --whole-graph single-chart page) gets its own line, because the
    generic loop below would send its user to two formats that emit
    something else."""
    if whole_graph and html:
        raise typer.Exit(
            refuse(
                "--html --whole-graph (the single-chart offline page, DL-70) was replaced"
                " by --format html-chart (DL-76)"
            )
        )
    removed = [
        (flag, mode)
        for flag, mode, passed in (
            ("--whole-graph", "chart", whole_graph),
            ("--html", "html", html),
            ("--explore", "explore", explore),
        )
        if passed
    ]
    if removed:
        for flag, mode in removed:
            typer.echo(f"{flag} was replaced by --format {mode}", err=True)
        raise typer.Exit(2)


def _refuse_undeliverable_viz_flags(*, collapse_threshold: int | None, fixed_scale: bool) -> None:
    """DL-75: refuse a shaping flag only where the chosen format cannot
    deliver its effect -- refusing one the user is getting anyway teaches
    nothing except to distrust the refusals. --elk/--fixed-scale stay silent
    under --format html, which already lays its charts out with ELK at
    natural scale; --format html-chart clears all five (same page defaults,
    and its one chart is to_mermaid's whole graph, so --collapse-threshold
    shapes it and every standalone job is on it already -- DL-76);
    --format explore passes that same test for --elk and
    --include-singletons (it always lays out with ELK, and always carries
    every standalone job -- search must find them). The two it cannot honor
    are below."""
    undeliverable = [
        (flag, reason)
        for flag, reason, passed in (
            (
                "--collapse-threshold",
                "every box is a compound node the canvas never collapses -- navigate"
                " (right-click a node to focus) instead of thinning the chart",
                collapse_threshold is not None,
            ),
            (
                "--fixed-scale",
                "the canvas fits its layout to the viewport and the operator zooms from"
                " there, so there is no emitted scale to fix",
                fixed_scale,
            ),
        )
        if passed
    ]
    if undeliverable:
        for flag, reason in undeliverable:
            typer.echo(f"{flag} cannot shape --format explore: {reason}.", err=True)
        typer.echo("Drop the option, or use --format html for a shaped offline page.", err=True)
        raise typer.Exit(2)


def viz(
    files: list[Path] = CATALOG_FILES,
    output_format: VizFormat = typer.Option(
        VizFormat.report,
        "--format",
        help="report: Markdown report of per-workflow charts (DL-35). "
        "chart: one bare Mermaid chart of the whole graph, standalone jobs "
        "included, --direction auto meaning LR (DL-61). "
        "html: one self-contained offline page, charts rendering in-browser "
        "with ELK layout at natural scale, ~5 MB of embedded JavaScript (DL-70). "
        "html-chart: that same offline page holding the whole-graph chart alone "
        "-- the terminal-artifact counterpart to chart, which is bare pipeable "
        "Mermaid text (DL-70, DL-76). "
        "explore: one self-contained interactive navigation page, ~2 MB of "
        "embedded JavaScript (DL-71). -o is recommended for every page format.",
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
    fixed_scale: bool = typer.Option(
        False,
        "--fixed-scale",
        help="Per-chart frontmatter flowchart.useMaxWidth=false: charts render at natural "
        "size (uniform scale across charts) instead of shrinking to fit the page. "
        "Composes with --elk into one frontmatter block.",
    ),
    out: Path = typer.Option(None, "--out", "-o", help="Write the report here, not stdout."),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
    # Removed booleans (DL-75), kept hidden only so passing one names its
    # replacement instead of dying with a bare "no such option".
    whole_graph: bool = typer.Option(False, "--whole-graph", hidden=True),
    html: bool = typer.Option(False, "--html", hidden=True),
    explore: bool = typer.Option(False, "--explore", hidden=True),
) -> None:
    """Render FILES' derived dependency graph in one of five exclusive
    formats -- see --format. The shaping options (--collapse-threshold,
    --direction, --include-singletons, --elk, --fixed-scale) apply wherever
    the chosen format can deliver their effect, and are refused where it
    cannot (DL-75)."""
    from dsl41.viz import DEFAULT_COLLAPSE_THRESHOLD, to_markdown, to_mermaid

    _refuse_removed_viz_flags(whole_graph, html, explore)
    if direction not in ("auto", "LR", "TD"):
        raise typer.Exit(refuse(f"--direction must be auto, LR, or TD, got {direction!r}"))
    if output_format is VizFormat.explore:
        _refuse_undeliverable_viz_flags(
            collapse_threshold=collapse_threshold,
            fixed_scale=fixed_scale,
        )
    catalog = load_catalog_or_exit_2(files, permit_unknown, properties)
    threshold = DEFAULT_COLLAPSE_THRESHOLD if collapse_threshold is None else collapse_threshold
    title = ", ".join(f.name for f in files)
    if output_format is VizFormat.explore:
        from dsl41.viz_explore import to_explore_html

        report = to_explore_html(
            catalog,
            title=title,
            direction=direction,  # type: ignore[arg-type]  # validated above
        )
    elif output_format is VizFormat.html:
        from dsl41.viz_html import to_html

        report = to_html(
            catalog,
            title=title,
            collapse_threshold=threshold,
            direction=direction,  # type: ignore[arg-type]  # validated above
            include_singletons=include_singletons,
        )
    elif output_format is VizFormat.html_chart:
        from dsl41.viz_html import to_html_chart

        report = to_html_chart(
            catalog,
            title=title,
            collapse_threshold=threshold,
            direction=direction,  # type: ignore[arg-type]  # validated above
        )
    elif output_format is VizFormat.chart:
        report = to_mermaid(
            catalog,
            collapse_threshold=threshold,
            direction="LR" if direction == "auto" else direction,  # type: ignore[arg-type]
            elk=elk,
            fixed_scale=fixed_scale,
        )
    else:
        report = to_markdown(
            catalog,
            title=title,
            collapse_threshold=threshold,
            direction=direction,  # type: ignore[arg-type]  # validated above
            include_singletons=include_singletons,
            elk=elk,
            fixed_scale=fixed_scale,
        )
    _emit(report, out)
