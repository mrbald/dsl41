"""The compiler verbs (DL-137): every verb that reads JIL and writes a finding, report, bundle,
module, chart or de-identified estate, and touches no run root and no socket.

The verbs are `lint`, `equiv`, `report`, `uc`, `decompile`, `build`, `minify`, `folds`,
`resolve` and `viz`. `build` is the one verb that reads Python: it imports an authored module and
writes the JIL its estate() declares. The exit-code contract is stated in `cli.py`.
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
    TIMEZONE_OPT,
    check_base_tz,
    load_catalog_or_exit_2,
    load_tz_aliases,
    parse_files_or_exit_2,
    refuse,
)
from dsl41.ir import CatalogIR
from dsl41.lint import lint_catalog
from dsl41.placeholders import PlaceholderError, load_properties, substitute

if TYPE_CHECKING:  # type-only: equiv's runtime import stays deferred
    from dsl41.dsl import CatalogBuilder
    from dsl41.minify import MinifyRefusal
    from dsl41.equiv import TierAResult, TierBCatalogResult, TierCResult
    from dsl41.viz import Direction


def _emit(body: str, out: "Path | None") -> None:
    """Write BODY to --out as bytes, or echo it to stdout (DL-178g).

    Bytes keep line endings exact: `write_text` newline-translates on Windows. The caller supplies
    BODY exactly as it should read in both places, including any trailing newline."""
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
    timezone: str = TIMEZONE_OPT,
    timezone_map: Path = typer.Option(
        None,
        "--timezone-map",
        help="Used only by L023. File that maps AutoSys timezone names to zones: the"
        " output of 'autotimezone -l', or plain 'name zone' lines. Without it, an unknown"
        " city name is read as the one matching zoneinfo city, without a warning.",
    ),  # SEM-35/DL-62
) -> None:
    """Check JIL files and report linter findings.

    All FILES form one catalog. Each finding names its rule, file and line.
    --timezone and --timezone-map read as in 'dsl41 run'; only L023 (a time
    in a DST gap or overlap hour) uses them.

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
    tz_aliases = load_tz_aliases(timezone_map)
    check_base_tz(timezone, tz_aliases)
    catalog = load_catalog_or_exit_2(files, permit_unknown, properties)
    report = lint_catalog(catalog, base_tz=timezone, tz_aliases=tz_aliases).suppress(codes)
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
                # tier b compares edges over set(A.jobs) & set(B.jobs), not the node list, so two
                # disjoint catalogs can both come back equivalent; tier (a) owns the job-set
                # question (ir-design ss6)
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
    # +"\n": `uc` is the one emitter whose body ends in a newline; `_emit` adds none
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
    properties: list[Path] = typer.Option(
        None,
        "--properties",
        "-p",
        help="Properties files with KEY=VALUE lines. Their values fill ~{$NAME}~"
        " placeholders before parsing. Repeatable; later files override earlier"
        " ones. The module then holds the bound values and cannot recover the"
        " ~{$NAME}~ tokens, so decompile the template JIL to keep them.",
    ),  # DL-19/DL-22
) -> None:
    """Turn JIL files into a Python module that rebuilds the same catalog.

    The module uses the dsl41 builder DSL. Its estate() function returns
    the builder, and 'dsl41 build' writes the JIL from it. The rebuilt
    catalog's canonical form equals the input's. With --check (the
    default), this command runs estate() and compares the two before you
    rely on it. The fold inventory and any construct left explicit are
    reported on stderr.

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
            permit_unknown=permit_unknown,
        )
    except DslError as exc:
        # a decompiler refusal is the same class as a lowering refusal: the input never became
        # output (DL-37a)
        raise typer.Exit(refuse(exc, prefix="decompile refused")) from exc
    # Emit BEFORE checking (DL-37a): the module survives for inspection if the check fails.
    _emit(source, out)
    for line in fold_report:
        typer.echo(f"fold: {line}", err=True)
    if check:
        from dsl41.equiv import catalog_hash, equivalent_tier_a

        namespace: dict[str, object] = {"__name__": "<decompiled>"}
        try:
            exec(compile(source, "<decompiled>", "exec"), namespace)  # noqa: S102
            rebuilt = namespace["estate"]().build(  # type: ignore[operator]
                permit_unknown=permit_unknown
            )
        except Exception as exc:
            typer.echo(
                "round-trip check FAILED (a decompiler gap, not your input):"
                f" the emitted module raised {type(exc).__name__}: {exc}",
                err=True,
            )
            raise typer.Exit(1) from exc
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


def _builder_from_module(module: Path) -> "CatalogBuilder":
    """Import MODULE from its path and return what its estate() returns, or exit 2 (DL-410).

    Until estate() returns, the module's directory leads sys.path, as it does for `python
    MODULE`, so an estate may import its sibling modules, at import or inside estate(). Nothing
    writes bytecode beside the source. The module is registered in sys.modules under a name no
    real module has, so code that looks itself up there (a dataclass under postponed
    annotations) works. Afterwards the module leaves sys.modules, and so does every module newly
    imported through the directory's sys.path entry: a sibling file, or a sibling package and
    its submodules. A later load in the same process then imports its own siblings. A module
    found through another sys.path entry stays, even one below the directory, such as a
    project's `.venv` or `src`."""
    import importlib.util
    import os
    import sys

    from dsl41.dsl import CatalogBuilder

    spec = importlib.util.spec_from_file_location(
        f"dsl41_build_{module.stem}_{os.getpid()}", module
    )
    if spec is None or spec.loader is None:
        raise typer.Exit(refuse(f"{module}: not a Python source file"))
    loaded = importlib.util.module_from_spec(spec)
    directory = module.resolve().parent
    known = set(sys.modules)
    sys.modules[spec.name] = loaded
    sys.path.insert(0, str(directory))
    write_bytecode, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        try:
            spec.loader.exec_module(loaded)
        except (Exception, SystemExit) as exc:
            raise typer.Exit(
                refuse(f"{module}: importing the module raised {type(exc).__name__}: {exc}")
            ) from exc
        function = getattr(loaded, "estate", None)
        if not callable(function):
            raise typer.Exit(refuse(f"{module}: the module defines no estate() function"))
        try:
            builder = function()
        except (Exception, SystemExit) as exc:
            raise typer.Exit(
                refuse(f"{module}: estate() raised {type(exc).__name__}: {exc}")
            ) from exc
    finally:
        sys.dont_write_bytecode = write_bytecode
        sys.path.remove(str(directory))
        for name in set(sys.modules) - known:
            origin = getattr(sys.modules[name], "__file__", None)
            top = directory / name.partition(".")[0]
            if name == spec.name or (
                origin is not None
                and (
                    Path(origin).resolve() == top.with_name(f"{top.name}.py")
                    or Path(origin).resolve().is_relative_to(top)
                )
            ):
                del sys.modules[name]
    if not isinstance(builder, CatalogBuilder):
        raise typer.Exit(
            refuse(f"{module}: estate() returned {type(builder).__name__}, not a CatalogBuilder")
        )
    return builder


def _jil_diff(current: bytes, data: bytes, fromfile: str, tofile: str) -> str:
    """A unified diff of two JIL texts that marks a missing final newline, as diff(1) does, and
    names a difference of line endings or trailing newlines alone (DL-410)."""
    import difflib

    lines: list[str] = []
    for line in difflib.unified_diff(
        current.decode("utf-8", errors="replace").splitlines(keepends=True),
        data.decode("utf-8").splitlines(keepends=True),
        fromfile=fromfile,
        tofile=tofile,
    ):
        lines.append(line)
        if not line.endswith("\n"):
            lines.append("\n\\ No newline at end of file\n")
    if current.replace(b"\r\n", b"\n").rstrip(b"\n") == data.replace(b"\r\n", b"\n").rstrip(b"\n"):
        lines.append("The two differ only in line endings or trailing newlines.\n")
    return "".join(lines)


def _write_atomic(target: Path, data: bytes) -> None:
    """Write DATA to a temporary file beside TARGET, then rename it over TARGET.

    A reader of TARGET sees the old bytes or the new bytes, never a partial file."""
    import os

    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def build(
    module: Path = typer.Argument(
        ..., help="Python file that defines estate() and returns a CatalogBuilder."
    ),
    out: Path = typer.Option(
        None,
        "--out",
        "-o",
        help="Write the JIL to this file. It is replaced whole by a plain file;"
        " a symlink there is replaced, not followed.",
    ),
    check: Path = typer.Option(
        None,
        "--check",
        help="Compare the JIL with this file instead of writing. A difference"
        " prints a unified diff.",
    ),
    stdout: bool = typer.Option(False, "--stdout", help="Print the JIL instead of writing."),
    permit_unknown: bool = PERMIT_UNKNOWN,
) -> None:
    """Write the JIL that a Python module's estate() declares.

    This command imports MODULE, so it executes that code: run it only
    on modules you trust. It calls estate(), parses and lowers the JIL
    through the compiler, and writes it. Give exactly one of --out,
    --check or --stdout. --out writes through a temporary file beside the
    target and a rename. --check writes nothing.

    Exit codes: 0 written, printed, or equal to the --check file; 1 the
    --check file differs; 2 MODULE could not be imported or has no
    estate(), estate() raised or returned something other than a
    CatalogBuilder, its JIL did not lower, or a file could not be read
    or written.
    """
    from dsl41.ast_jil import JilParseError
    from dsl41.ir import LoweringError

    if sum((out is not None, check is not None, stdout)) != 1:
        raise typer.Exit(refuse("give exactly one of --out, --check or --stdout"))
    builder = _builder_from_module(module)
    try:
        builder.build(permit_unknown=permit_unknown)
    except (JilParseError, LoweringError) as exc:
        raise typer.Exit(refuse(exc, prefix=f"{module}: the built JIL does not lower")) from exc
    data = builder.to_jil().encode("utf-8")
    if stdout:
        typer.echo(data.decode("utf-8"), nl=False)
        return
    try:
        if check is None:
            assert out is not None
            _write_atomic(out, data)
            typer.echo(f"wrote {out}")
            return
        current = check.read_bytes()
    except OSError as exc:
        raise typer.Exit(refuse(exc)) from exc
    if current == data:
        typer.echo(f"{check} matches {module}")
        return
    typer.echo(_jil_diff(current, data, str(check), f"{module} estate()"), nl=False)
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
    """`viz --direction`: the chart directions plus auto, which picks one per component."""

    auto = "auto"
    LR = "LR"
    TD = "TD"


class VizFormat(str, Enum):
    """The five viz outputs, exclusive by construction (DL-75); html-chart is a value (DL-76)."""

    report = "report"
    chart = "chart"
    html = "html"
    html_chart = "html-chart"
    explore = "explore"


def _refuse_removed_viz_flags(whole_graph: bool, html: bool, explore: bool) -> None:
    """DL-75: refuse the three removed mode booleans and name the replacement format.

    The --html --whole-graph combination (DL-70(4)) gets its own line, since the generic loop
    would send its user to two formats that emit something else."""
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
    """DL-75: refuse a shaping flag only where the chosen format cannot deliver its effect.

    --elk/--fixed-scale are silent under --format html, which already lays out with ELK at natural
    scale. --format html-chart clears all five: its one chart is to_mermaid's whole graph, so
    --collapse-threshold shapes it and every standalone job is on it (DL-76). --format explore
    clears --elk and --include-singletons (it always lays out with ELK and carries every
    standalone job) and --collapse-threshold (DL-190; the page folds over-threshold boxes
    before its first layout). One flag is left, stated once (DL-193)."""
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
    # Removed booleans (DL-75), hidden so passing one names its replacement.
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
    # One name for the option all the way down. The report's default of 12 is the four Mermaid
    # formats'; `--format explore` keeps None, "fold nothing" (DL-71, DL-190).
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
    """One refusal block on stderr, led by the warning that the quoted estate text must not travel.

    The block quotes the value and file:line so the owner can act. Printed once, not per line.
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
            # a --out naming a FILE, or a path under one, is an OSError from mkdir
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
