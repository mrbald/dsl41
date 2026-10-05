"""The compiler verbs: a catalog in, an artifact out (DL-137's split).

`lint`, `equiv`, `report`, `uc`, `decompile`, `minify`, `folds`, `resolve`
and `viz` -- every verb that reads JIL and writes a finding, a report, a
bundle, a module, a chart or a de-identified estate, and touches no run root
and no socket. Registered on
the app in `cli.py`; the exit-code contract is stated there.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import typer

from dsl41.cli_common import (
    CATALOG_FILES,
    PERMIT_UNKNOWN,
    PROPERTIES,
    load_catalog_or_exit_2,
    parse_files_or_exit_2,
    refuse,
)
from dsl41.ir import CatalogIR
from dsl41.lint import lint_catalog
from dsl41.placeholders import PlaceholderError, load_properties, substitute

if TYPE_CHECKING:  # type-only: equiv's runtime import stays deferred (below)
    from dsl41.minify import MinifyRefusal
    from dsl41.equiv import TierAResult, TierBCatalogResult, TierCResult
    from dsl41.viz import Direction


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
    strict: bool = typer.Option(False, "--strict", help="Also fail on warnings."),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
    suppress: list[str] = typer.Option(
        [],
        "--suppress",
        help="Rule codes to leave out of the report and the exit code, for"
        " example L005. Repeatable; comma-separated lists accepted.",
    ),  # DL-23
) -> None:
    """Check JIL files and report linter findings.

    All FILES form one catalog. Each finding names its rule, file and line.

    Exit codes: 0 clean; 1 findings at error severity, or at warning
    severity with --strict; 2 the input could not be read, parsed or
    lowered.
    """
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


class EquivTier(str, Enum):
    """`equiv --tier`: one tier, or all three."""

    a = "a"
    b = "b"
    c = "c"
    all = "all"


def equiv(
    files: list[Path] = typer.Argument(..., help="JIL files of catalog A."),
    against: list[Path] = typer.Option(
        ..., "--against", "-b", help="JIL files of catalog B. Repeatable."
    ),
    tier: EquivTier = typer.Option(EquivTier.all, "--tier", help="Tiers to run: a, b, c or all."),
    rename: list[str] = typer.Option(
        [], "--rename", help="Job name mapping from A to B as OLD=NEW. Repeatable."
    ),
    case_fold: bool = typer.Option(
        False, "--case-fold", help="Compare job names case-insensitively."
    ),  # ir-design ss6
    scripts: int = typer.Option(
        20,
        "--scripts",
        help="Number of event scripts tier c generates. Seeded, so repeated runs agree.",
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Check whether two sets of JIL files describe the same estate.

    FILES form catalog A and --against forms catalog B. Three tiers
    compare them. Tier a: structural equality of the canonical form. Tier
    b: each job's condition logic and the derived graph. Tier c: oracle
    traces on shared event scripts. --properties applies the same
    bindings to both catalogs.

    Exit codes: 0 every requested tier reports equivalence; 1 a tier
    found a difference; 2 either input could not be read, parsed or
    lowered.
    """
    from dsl41.equiv import (
        RenameError,
        catalog_hash,
        equiv_scripts,
        equivalent_tier_a,
        equivalent_tier_b,
        equivalent_tier_c,
    )

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
        if tier in (EquivTier.a, EquivTier.all):
            divergent |= _print_tier_a(
                equivalent_tier_a(catalog_a, catalog_b, rename=rename_map, case_fold=case_fold)
            )
        if tier in (EquivTier.b, EquivTier.all):
            divergent |= _print_tier_b(
                equivalent_tier_b(catalog_a, catalog_b, rename=rename_map, case_fold=case_fold)
            )
            if tier is EquivTier.b:
                # tier b reads set(A.jobs) & set(B.jobs) and compares edges,
                # not the node list: two disjoint catalogs can both come back
                # equivalent. Tier (a) owns the job-set question (ir-design
                # ss6), so say so rather than let "equivalent" over-read.
                typer.echo(
                    "  note: tier b compares only the jobs both catalogs define;"
                    " a job present in one catalog alone is tier (a)'s question"
                    " (ir-design ss6) -- run --tier a or --tier all to settle it"
                )
        if tier in (EquivTier.c, EquivTier.all):
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
        None, "--out", "-o", help="Write the report to this file instead of stdout."
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Write the migration report for a set of JIL files (Markdown).

    The report lists what the compiler refused, assumed or warned about.
    It is the record of those findings, so it exits 0 whenever it was
    generated. Use 'dsl41 lint --strict' as a pass or fail gate.

    Exit codes: 0 report generated; 2 the input could not be read, parsed
    or lowered.
    """
    from dsl41.backend_uc import render_migration_report

    catalog = load_catalog_or_exit_2(files, permit_unknown, properties)
    markdown = render_migration_report(catalog)
    _emit(markdown, out)


def uc(
    files: list[Path] = CATALOG_FILES,
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the JSON bundle to this file instead of stdout."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit 1 when any workflow was quarantined."
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Convert JIL files into Stonebranch Universal Controller records (JSON).

    The bundle holds one taskWorkflow record per workflow, in the base
    create-only schema (docs/uc-edge-schema.md). A workflow is
    quarantined whole when it has an edge the base schema cannot
    express, or when its record name collides with another workflow's.
    Quarantined workflows are listed in the bundle's ledger and
    summarized on stderr.

    Exit codes: 0 bundle generated; 1 with --strict when any workflow
    was quarantined; 2 the input could not be read, parsed or lowered.
    """
    # Design: U3a
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
        None, "--out", "-o", help="Write the Python module to this file instead of stdout."
    ),
    check: bool = typer.Option(
        True,
        "--check/--no-check",
        help="Run the written module and compare the rebuilt catalog's canonical"
        " hash with the input's.",
    ),
    no_fold: list[str] = typer.Option(
        [],
        "--no-fold",
        help="Fold codes to disable, for example T-005. Repeatable;"
        " comma-separated lists accepted. 'dsl41 folds' lists the codes.",
    ),  # DL-38
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
) -> None:
    """Turn JIL files into a Python module that rebuilds the same catalog.

    The module uses the dsl41 builder DSL. Running it rebuilds a catalog
    whose canonical form equals the input's. With --check (the default),
    this command runs the module and compares the two before you rely on
    it. The fold inventory and any construct left explicit are reported
    on stderr.

    Exit codes: 0 module written; 1 the check found a difference (the
    module is still written, and the difference is a decompiler bug
    worth reporting); 2 the input could not be read, parsed or lowered,
    or the decompiler refused it (nothing to emit, or an unknown
    --no-fold code).
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
    """List the decompiler's fold patterns and their codes.

    Exit code: always 0.
    """
    # Design: DL-38; the phase-10 DSL (DL-03)
    from dsl41.dsl import FOLDS

    for code, description in FOLDS.items():
        typer.echo(f"{code}  {description}")


def resolve(
    files: list[Path] = typer.Argument(
        ...,
        help="Templated JIL files, or any text files. Several files are joined in order.",
    ),
    properties: list[Path] = typer.Option(
        ...,
        "--properties",
        "-p",
        help="Properties files with KEY=VALUE lines. Repeatable; later files"
        " override earlier ones.",
    ),
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the resolved text to this file instead of stdout."
    ),
    permit_unresolved: bool = typer.Option(
        False,
        "--permit-unresolved",
        help="Leave unresolved or malformed ~{...}~ tokens as they are and"
        " report them on stderr, instead of failing.",
    ),
) -> None:
    """Fill ~{$NAME}~ placeholders in JIL files from properties files.

    This is the estate's templating step, run ahead of the compiler.
    Several FILES are joined in argument order into one output. A
    missing newline between two inputs is added in the style of the text
    so far. Mixing LF and CRLF files is refused.

    Exit codes: 0 resolved, including permitted leftovers, which are
    reported on stderr; 2 a properties file or an input could not be
    resolved.
    """
    # Design: DL-19/DL-22
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


class VizDirection(str, Enum):
    """`viz --direction`: the chart directions plus auto, which picks one per
    component. The values are `viz.Direction` and "auto"."""

    auto = "auto"
    LR = "LR"
    TD = "TD"


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


def _refuse_undeliverable_viz_flags(*, fixed_scale: bool) -> None:
    """DL-75: refuse a shaping flag only where the chosen format cannot
    deliver its effect -- refusing one the user is getting anyway teaches
    nothing except to distrust the refusals. --elk/--fixed-scale stay silent
    under --format html, which already lays its charts out with ELK at
    natural scale; --format html-chart clears all five (same page defaults,
    and its one chart is to_mermaid's whole graph, so --collapse-threshold
    shapes it and every standalone job is on it already -- DL-76);
    --format explore passes that same test for --elk and
    --include-singletons (it always lays out with ELK, and always carries
    every standalone job -- search must find them), and since DL-190 for
    --collapse-threshold too: the page folds the over-threshold boxes before
    its first layout, the report's own rule. That leaves ONE, so it is
    stated once rather than kept as a table of one row (DL-193);
    `_refuse_removed_viz_flags` above earns its table with three."""
    if not fixed_scale:
        return
    typer.echo(
        "--fixed-scale cannot shape --format explore: the canvas fits its layout to the"
        " viewport and the operator zooms from there, so there is no emitted scale to fix.",
        err=True,
    )
    typer.echo("Drop the option, or use --format html for a shaped offline page.", err=True)
    raise typer.Exit(2)


def viz(
    files: list[Path] = CATALOG_FILES,
    output_format: VizFormat = typer.Option(
        VizFormat.report,
        "--format",
        metavar="FORMAT",
        help="report: a Markdown report with one chart per workflow. chart: one"
        " bare Mermaid chart of the whole graph, standalone jobs included."
        " html: a self-contained offline page that renders the charts in"
        " the browser (about 5 MB). html-chart: the same page with only"
        " the whole-graph chart. explore: a self-contained interactive"
        " page for navigating the graph (about 2 MB). Use -o for every"
        " page format.",
        # DL-35, DL-61, DL-70, DL-70/DL-76, DL-71
    ),
    collapse_threshold: int = typer.Option(
        None,
        "--collapse-threshold",
        help="Boxes with more direct members than this are drawn as one node."
        " Under --format explore they start collapsed instead, and only"
        " when this flag is given.",
        show_default="12; no folds under --format explore",
    ),
    direction: VizDirection = typer.Option(
        VizDirection.auto,
        "--direction",
        help="Chart direction: auto, LR or TD. auto chooses per component in the"
        " report, html and explore formats, and means LR for chart and html-chart.",
    ),
    include_singletons: bool = typer.Option(
        False,
        "--include-singletons",
        help="Also chart standalone jobs. They are always listed in Appendix A.",
    ),
    elk: bool = typer.Option(
        False,
        "--elk",
        help="Add Mermaid ELK layout frontmatter. VS Code and local renderers"
        " use it; GitHub ignores it.",
    ),
    fixed_scale: bool = typer.Option(
        False,
        "--fixed-scale",
        help="Render each chart at its natural size instead of shrinking it to"
        " fit the page. Combines with --elk into one frontmatter block.",
    ),
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the output to this file instead of stdout."
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    properties: list[Path] = PROPERTIES,
    # Removed booleans (DL-75), kept hidden only so passing one names its
    # replacement instead of dying with a bare "no such option".
    whole_graph: bool = typer.Option(False, "--whole-graph", hidden=True),
    html: bool = typer.Option(False, "--html", hidden=True),
    explore: bool = typer.Option(False, "--explore", hidden=True),
) -> None:
    """Draw the dependency graph as Markdown, Mermaid or an HTML page.

    All FILES form one catalog. --format picks the output. The shaping
    options (--collapse-threshold, --direction, --include-singletons,
    --elk, --fixed-scale) apply where the chosen format can honour them
    and are refused where it cannot.

    Exit codes: 0 output written; 2 the input could not be read, parsed
    or lowered, or a shaping option was refused for the chosen format.
    """
    # Design: DL-75
    from dsl41.viz import DEFAULT_COLLAPSE_THRESHOLD, to_markdown, to_mermaid

    _refuse_removed_viz_flags(whole_graph, html, explore)
    # The Enum is the closed set; `Direction | auto` is the same set the
    # renderers take (a test keeps the two equal).
    chosen = cast("Direction | Literal['auto']", direction.value)
    if output_format is VizFormat.explore:
        _refuse_undeliverable_viz_flags(fixed_scale=fixed_scale)
    catalog = load_catalog_or_exit_2(files, permit_unknown, properties)
    # One name for the option all the way down. The report's default of 12 is
    # the four Mermaid formats'; `--format explore` keeps None, which means
    # "fold nothing" -- the page opens on the whole graph (DL-71, DL-190).
    if output_format is not VizFormat.explore and collapse_threshold is None:
        collapse_threshold = DEFAULT_COLLAPSE_THRESHOLD
    title = ", ".join(f.name for f in files)
    if output_format is VizFormat.explore:
        from dsl41.viz_explore import to_explore_html

        report = to_explore_html(
            catalog,
            title=title,
            direction=chosen,
            collapse_threshold=collapse_threshold,
        )
    elif output_format is VizFormat.html:
        from dsl41.viz_html import to_html

        report = to_html(
            catalog,
            title=title,
            collapse_threshold=collapse_threshold,
            direction=chosen,
            include_singletons=include_singletons,
        )
    elif output_format is VizFormat.html_chart:
        from dsl41.viz_html import to_html_chart

        report = to_html_chart(
            catalog,
            title=title,
            collapse_threshold=collapse_threshold,
            direction=chosen,
        )
    elif output_format is VizFormat.chart:
        report = to_mermaid(
            catalog,
            collapse_threshold=collapse_threshold,
            direction="LR" if chosen == "auto" else chosen,
            elk=elk,
            fixed_scale=fixed_scale,
        )
    else:
        report = to_markdown(
            catalog,
            title=title,
            collapse_threshold=collapse_threshold,
            direction=chosen,
            include_singletons=include_singletons,
            elk=elk,
            fixed_scale=fixed_scale,
        )
    _emit(report, out)


def _print_refusal(exc: "MinifyRefusal") -> None:
    """One refusal block on stderr, led by what it is made of.

    A refusal has to quote the value and the file:line or the owner cannot act
    on it -- but that quote is estate text, and the whole point of the command
    is that estate text does not travel. So the block says so before it says
    anything else. Printed once, not per line.
    """
    typer.echo(
        "minify refused. The lines below QUOTE THE ESTATE (values, file names,"
        " line numbers): they are as sensitive as the input. Do not paste them"
        " into a public issue, a chat or a commit message.",
        err=True,
    )
    for message in exc.messages:
        typer.echo(f"  {message}", err=True)


def minify(
    files: list[Path] = typer.Argument(..., help="JIL source files to minify."),
    out: Path = typer.Option(
        None,
        "--out",
        "-o",
        help="Directory for the output, one minified file per input. Without"
        " it the whole result goes to stdout.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files under --out."),
    mapping: Path = typer.Option(
        None,
        "--mapping",
        help="Write the old-to-new name map to this JSON file. The map"
        " re-identifies the estate. Never commit or share it.",
    ),
    verify: bool = typer.Option(
        True,
        "--verify/--no-verify",
        help="Lower both estates and check they are isomorphic under the"
        " name map before writing anything.",
    ),
    scrub_timezones: bool = typer.Option(
        False,
        "--scrub-timezones",
        help="Replace every timezone with UTC. Off by default: zone names"
        " are public vocabulary, but a zone does reveal a region.",
    ),  # SEM-35
    properties: list[Path] = PROPERTIES,
) -> None:
    """Write a de-identified copy of JIL files that keeps their structure.

    What the compiler models survives: the job graph, conditions and
    their lookbacks, schedules, exit-code policy, resource gates and
    numeric timing hints. What could identify the estate does not:
    names are replaced with synthetic ones, 'command' becomes a
    constant, comments and observability attributes are dropped, and
    every kept value is checked against the closed set its key allows.
    An attribute this tool cannot classify stops the run.

    The leak guard is a backstop, not a complete check. It only sees
    tokens of four or more characters that contain a letter, and it
    cannot see a value the classification table treats as closed
    vocabulary. Read the output before you share it.

    --properties fills ~{$NAME}~ placeholders before parsing, so a
    placeholder in a kept value is checked as its bound value. Without
    it a placeholder is plain text, and one in a kept value is refused.
    A properties file is as sensitive as the JIL it fills.

    Exit codes: 0 output written; 2 the input could not be read or
    parsed, or a placeholder could not be resolved; 3 the tool refused
    to emit (an unclassified key or subcommand, a value that does not
    parse, a structural mismatch with the original, a surviving input
    token, a kept value outside its closed set, or an existing output
    file without --force). A refusal quotes the offending text on
    stderr.
    """
    from dsl41.minify import MinifyRefusal, minify_files, output_paths

    parsed, _fingerprint = parse_files_or_exit_2(files, properties)
    try:
        targets = output_paths(list(files), out) if out is not None else []
        if not force:
            existing = [t for t in [*targets, *([mapping] if mapping else [])] if t.exists()]
            if existing:
                raise MinifyRefusal(
                    [f"{path} exists (use --force to overwrite)" for path in existing]
                )
        result = minify_files(parsed, verify=verify, scrub_timezones=scrub_timezones)
        if out is not None:
            # A --out that names a FILE, or a path under one, is an OSError from
            # mkdir; without this it left the surface as an exit-1 traceback.
            out.mkdir(parents=True, exist_ok=True)
    except MinifyRefusal as exc:
        _print_refusal(exc)
        raise typer.Exit(3) from exc
    except OSError as exc:
        typer.echo(f"--out: {exc}", err=True)
        raise typer.Exit(3) from exc
    zones = result.kept_timezones()
    if zones and not scrub_timezones:
        typer.echo(
            f"note: the output keeps {len(zones)} timezone(s): {', '.join(zones)}."
            " A zone discloses a region -- pass --scrub-timezones to map them all"
            " to UTC.",
            err=True,
        )
    if out is None:
        typer.echo("\n".join(result.bodies), nl=False)
    else:
        for target, body in zip(targets, result.bodies, strict=True):
            target.write_bytes(body.encode("utf-8"))
            typer.echo(f"wrote {target}", err=True)
        typer.echo(
            "note: output FILENAMES are the input basenames and are not minified;"
            " rename them if the file names themselves name the estate.",
            err=True,
        )
    if mapping is not None:
        mapping.write_bytes(result.names.to_json().encode("utf-8"))
        typer.echo(
            f"wrote {mapping}: this file RE-IDENTIFIES the estate. Never commit it,"
            " never share it with the minified output.",
            err=True,
        )
