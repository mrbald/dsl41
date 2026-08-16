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

import hashlib
from collections.abc import Iterable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, cast

import typer

from dsl41.ast_jil import JilFile, JilParseError, parse, render_statement
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
    return _load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)[0]


def _load_catalog_and_ast_or_exit_2(
    files: Iterable[Path],
    permit_unknown: bool,
    properties: list[Path] | None = None,
) -> tuple[CatalogIR, list[JilFile], dict[str, str]]:
    """Returns (catalog, parsed ASTs, input fingerprint). The fingerprint --
    path -> sha256 -- is the ss10 spec_drift baseline (DL-65) and hashes the
    SAME bytes this load parsed (review: a separate re-read could baseline
    bytes the run never loaded, inverting the drift hint's one job), inside
    the same guarded try so an unreadable input stays an exit-2 refusal."""
    try:
        parsed: list[JilFile] = []
        fingerprint: dict[str, str] = {}
        bindings = load_properties(properties) if properties else None
        for path in properties or []:
            fingerprint[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files:
            data = path.read_bytes()
            fingerprint[str(path)] = hashlib.sha256(data).hexdigest()
            text = data.decode("utf-8")
            if bindings is not None:
                text, _ = substitute(text, bindings, file=str(path))
            parsed.append(parse(text, file=str(path)))
        return lower_catalog(parsed, permit_unknown=permit_unknown), parsed, fingerprint
    except (JilParseError, LoweringError, PlaceholderError, OSError, UnicodeDecodeError) as exc:
        # OSError/UnicodeDecodeError: unreadable input (missing file, directory,
        # non-UTF-8) never reached the tool -- same exit-2 class as a refusal.
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


def _write_manifest(
    run_root: Path,
    parsed: "list[JilFile]",
    catalog: CatalogIR,
    fingerprint: "dict[str, str]",
    options: "dict[str, object]",
) -> None:
    """DL-66 (review finding: old runs were not self-contained artifacts).
    manifest/ preserves the POST-PLACEHOLDER source this run loaded
    (render_preserve is byte-exact, F1) plus manifest.json: tool version,
    catalog hash, sha256 of every input, original paths, launch options.
    The manifest sources are valid JIL needing no -p; the catalog hash
    covers SourceSpan.file, so byte-exact resume/replay against them also
    needs the original paths recorded here (relocation-independent hashing
    is a DELIBERATE defer -- changing the hash orphans every existing
    journal's resume gate; it needs its own versioned migration)."""
    import json as json_mod
    import os
    from datetime import UTC, datetime

    from dsl41.ast_jil import render_preserve
    from dsl41.runner_journal import _dsl41_version, catalog_hash

    manifest_dir = run_root / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(manifest_dir, 0o700)
    sources: list[dict[str, str]] = []
    used: set[str] = set()
    for jf in parsed:
        base = Path(jf.file).name or "estate.jil"
        name, n = base, 1
        while name in used:
            n += 1
            name = f"{n:02d}-{base}"
        used.add(name)
        (manifest_dir / name).write_text(render_preserve(jf))
        os.chmod(manifest_dir / name, 0o600)
        sources.append({"file": name, "original_path": jf.file})
    payload = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dsl41_version": _dsl41_version(),
        "catalog_hash": catalog_hash(catalog),
        "inputs_sha256": fingerprint,
        "sources": sources,
        "options": options,
    }
    (manifest_dir / "manifest.json").write_text(
        json_mod.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    os.chmod(manifest_dir / "manifest.json", 0o600)


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
def uc(
    files: list[Path] = typer.Argument(
        ..., help="JIL files / autocal calendar exports forming one catalog"
    ),
    out: Path = typer.Option(
        None, "--out", "-o", help="Write the JSON bundle here instead of stdout."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit 1 when any workflow was quarantined."
    ),
    permit_unknown: bool = _PERMIT_UNKNOWN,
    properties: list[Path] = _PROPERTIES,
) -> None:
    """Emit the U3a base CREATE-ONLY UC workflow record bundle (JSON).

    One taskWorkflow record per serializable workflow, exactly the shape
    frozen in docs/uc-edge-schema.md; workflows the base schema cannot
    express are QUARANTINED whole and listed in the bundle's own ledger
    (summarized on stderr). Exit 0 once a bundle is generated (1 with
    --strict when anything was quarantined); exit 2 when the input never
    reached the backend.
    """
    from dsl41.backend_uc import compile_to_uc

    catalog = _load_catalog_or_exit_2(files, permit_unknown, properties)
    bundle = compile_to_uc(catalog)
    text = bundle.model_dump_json(indent=2)
    if out is None:
        typer.echo(text)
    else:
        out.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"wrote {out}")
    typer.echo(
        f"{len(bundle.records)} record(s); {len(bundle.quarantined)} quarantined",
        err=True,
    )
    for workflow in bundle.quarantined:
        for reason in workflow.reasons:
            typer.echo(f"quarantined {workflow.name}: {reason}", err=True)
    if strict and bundle.quarantined:
        raise typer.Exit(1)


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
    from datetime import datetime as datetime_mod

    from dsl41.oracle import Oracle
    from dsl41.oracle_state import OracleError
    from dsl41.runner_clock import EngineError
    from dsl41.runner_hosts import LOCAL_EXECUTOR_ID, seed_local_executor
    from dsl41.runner_journal import catalog_hash, read_journal, replay_inputs

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
    # reproducing a log means reproducing the genesis the engine replayed it
    # onto, not only the catalog: a routing-table input lands on a table that
    # already holds this engine's own executor (concurrency-model ss8), and a
    # replay without it would decide "no such host" where the run decided
    # otherwise. The stamp only reaches `last_contact`, which no input reads.
    seed_local_executor(
        oracle.store,
        LOCAL_EXECUTOR_ID,
        at=datetime_mod.fromisoformat(str(header["started_at"])),
    )
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
        typer.echo(
            "--html --whole-graph (the single-chart offline page, DL-70) was replaced"
            " by --format html-chart (DL-76)",
            err=True,
        )
        raise typer.Exit(2)
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


@app.command()
def viz(
    files: list[Path] = typer.Argument(
        ..., help="JIL files / autocal calendar exports forming one catalog"
    ),
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
    permit_unknown: bool = _PERMIT_UNKNOWN,
    properties: list[Path] = _PROPERTIES,
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
        typer.echo(f"--direction must be auto, LR, or TD, got {direction!r}", err=True)
        raise typer.Exit(2)
    if output_format is VizFormat.explore:
        _refuse_undeliverable_viz_flags(
            collapse_threshold=collapse_threshold,
            fixed_scale=fixed_scale,
        )
    catalog = _load_catalog_or_exit_2(files, permit_unknown, properties)
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
#
# `sendevent` splits 2 further, because since S3 a command can fail in three
# ways that call for three different next moves and a script that cannot tell
# them apart has to guess exactly where guessing costs most (DL-92): 2 stays
# REFUSED (nothing admitted, nothing logged -- the "never started" reading of
# 2, unchanged), 3 is REJECTED (a decision, with an index; the world moved)
# and 4 is UNKNOWN (no decision arrived; it may yet apply). 1 keeps its
# meaning and no other verb uses 3 or 4.


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

_TIMEZONE_MAP_OPT = typer.Option(
    None,
    "--timezone-map",
    help="File resolving vendor timezone names (SEM-35/DL-62): the instance's"
    " `autotimezone -l` listing verbatim, or bare 'name zone' pairs. Without"
    " it, an unknown city name falls back to the unique zoneinfo city match"
    " with a preflight WARN.",
)


def _load_tz_aliases(path: "Path | None") -> "dict[str, str] | None":
    """Parse --timezone-map (DL-62); unreadable/malformed exits 2."""
    if path is None:
        return None
    from dsl41.runner_scheduler import parse_timezone_map

    try:
        return parse_timezone_map(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.echo(f"--timezone-map {path}: {exc}", err=True)
        raise typer.Exit(2) from exc


def _check_base_tz(timezone: str | None, tz_aliases: "dict[str, str] | None" = None) -> None:
    """Preflight the run-level base zone: per-job zones gate in ss8, but the
    --timezone flag would otherwise surface as a raw traceback from the
    Scheduler with the wrong exit code (DL-45)."""
    if timezone is None:
        return
    from dsl41.runner_scheduler import resolve_timezone

    if resolve_timezone(timezone, tz_aliases) is None:
        typer.echo(
            f"--timezone {timezone!r} is not resolvable (SEM-35: zoneinfo, the"
            " --timezone-map table, or a POSIX fixed offset)",
            err=True,
        )
        raise typer.Exit(2)


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
    timezone: str = _TIMEZONE_OPT,
    timezone_map: Path = _TIMEZONE_MAP_OPT,
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

    from datetime import UTC, datetime

    if ui:
        _import_tui_or_exit_2()  # fail before the engine starts, not after
    catalog, parsed, fingerprint = _load_catalog_and_ast_or_exit_2(
        files, permit_unknown, properties
    )
    tz_aliases = _load_tz_aliases(timezone_map)
    warns = _preflight_or_exit(
        catalog,
        execution=True,
        machine_policy=machine_policy,
        as_machine=as_machine,
        start=datetime.now(UTC).replace(tzinfo=None),
        tz_aliases=tz_aliases,
    )
    _check_base_tz(timezone, tz_aliases)
    if deadman is not None and not detached:
        # loud, not silent: without a supervisor there is nothing to hold the
        # lifelines, so nothing a deadman could bound (concurrency-model ss8)
        typer.echo("--deadman needs --detached: a tethered run has no supervisor", err=True)
        raise typer.Exit(2)
    if deadman is not None and deadman <= 0:
        typer.echo("--deadman must be a positive number of seconds", err=True)
        raise typer.Exit(2)
    from dsl41.runner_clock import EngineError

    if not resume and not (run_root / "journal.jsonl").exists():
        # a used run root is start_run's refusal to make -- never repaint
        # its manifest on the way to that refusal
        _write_manifest(
            run_root,
            parsed,
            catalog,
            fingerprint,
            {
                "files": [str(f) for f in files],
                "properties": [str(p) for p in (properties or [])],
                "run_root": str(run_root),
                "detached": detached,
                "machine_policy": machine_policy,
                "as_machine": list(as_machine),
                "timezone": timezone,
            },
        )
    try:
        raise typer.Exit(
            asyncio.run(
                _serve_run(
                    catalog,
                    run_root,
                    resume,
                    timezone,
                    warns,
                    ui,
                    detached,
                    deadman,
                    tz_aliases,
                    spec_texts=_spec_texts(parsed, catalog),
                    estate_fingerprint=fingerprint,
                )
            )
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
    timezone: str | None,
    warns: list,
    ui: bool = False,
    detached: bool = False,
    deadman: "float | None" = None,
    tz_aliases: "dict[str, str] | None" = None,
    spec_texts: "dict[str, str] | None" = None,
    estate_fingerprint: "dict[str, str] | None" = None,
) -> int:
    import asyncio
    import contextlib
    import signal as signal_mod

    from datetime import datetime

    from dsl41.runner_startup import start_run
    from dsl41.runner_control import ControlServer
    from dsl41.runner_startup import resume_run as _resume_run
    from dsl41.runner_adapters import (
        FileWatcherAdapter,
        JobAdapter,
        LocalCommandAdapter,
        SupervisedCommandAdapter,
        SupervisorClient,
        SupervisorUnavailable,
    )
    from dsl41.runner_clock import EngineError, RealClock
    from dsl41.runner_scheduler import Scheduler

    from dsl41.runner_ledger import acquire_run_root

    clock = RealClock()
    # ACQUIRE first (S6a, concurrency-model ss7). Earlier than the engine's
    # own entry points would, because the next thing this function does is
    # START a supervisor and take its lease -- an act on an estate this
    # process may turn out not to lead.
    try:
        lock = acquire_run_root(run_root)
    except EngineError as exc:
        typer.echo(str(exc), err=True)
        return 2
    # detached (ss6a Tier 1, spec ss3): the CMD adapter SPAWNs through a
    # supervisor that owns the wrapper lifelines, so an engine restart does
    # not kill the jobs. FW stays in-engine (no process to survive).
    client: SupervisorClient | None = None
    if detached:
        client = SupervisorClient(run_root, deadman_s=deadman)
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
    scheduler = Scheduler(catalog, start=clock.now(), default_tz=timezone, tz_aliases=tz_aliases)
    running_deadman = _running_deadman(client, deadman, run_root)
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
        )
    if client is not None:
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
    timezone_map: Path = _TIMEZONE_MAP_OPT,
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

    from dsl41.oracle_state import Event, OracleError
    from dsl41.runner import Engine
    from dsl41.runner_startup import start_run
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import EngineError, VirtualClock
    from dsl41.runner_scheduler import Scheduler

    catalog = _load_catalog_or_exit_2(files, permit_unknown, properties)
    start_dt = (
        _naive_utc_arg(start, "--start")
        if start
        else datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    )
    tz_aliases = _load_tz_aliases(timezone_map)
    warns = _preflight_or_exit(catalog, execution=False, start=start_dt, tz_aliases=tz_aliases)
    _check_base_tz(timezone, tz_aliases)
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
    scheduler = Scheduler(catalog, start=start_dt, default_tz=timezone, tz_aliases=tz_aliases)
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
    """Exit-code shell around runner_control.roundtrip (DL-78): the protocol
    client raises, the CLI decides that a failed READ is exit 2.

    Reads only. A read that did not answer changed nothing whether or not it
    was delivered, so it has one outcome; a MUTATION has four, and takes
    `_mutate` below."""
    from dsl41.runner_control import ControlClientError, roundtrip

    try:
        return roundtrip(socket_path, request)
    except ControlClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


def _no_decision(request: dict) -> None:
    """DL-92's fourth outcome, said out loud. The id is on stderr because it
    is the only thing that makes the retry safe, and a caller that lost the
    round trip has nowhere else to get it: the answer that would have
    carried it never came."""
    typer.echo(
        f"no decision: this command may still apply. Re-read, then retry ONLY as"
        f" --request-id {request['request_id']}",
        err=True,
    )


def _mutate(socket_path: Path, request: dict) -> None:
    """Send one ss6 command envelope and exit on its outcome: DL-92's four
    (0 applied / 2 refused / 3 rejected / 4 unknown).

    One helper for `sendevent` and `host` because reading an answer is the
    PROTOCOL's business, not the verb's -- and because the transport half
    has to read the same way at both, which is the half that was wrong. A
    dropped connection used to land on exit 2 wherever it happened, and 2
    promises the log says nothing about the command and it is safe to send
    again unchanged. That promise holds for a request that never left; for
    one that left and got no answer it is exactly backwards, because the
    engine fsyncs an attempt before it feeds it. Delivered-and-unanswered is
    the case `unknown` exists for."""
    import json as json_mod

    from dsl41.runner_control import (
        REFUSED,
        REJECTED,
        UNKNOWN,
        ControlClientError,
        outcome_of,
        roundtrip,
    )

    try:
        response = roundtrip(socket_path, request)
    except ControlClientError as exc:
        typer.echo(str(exc), err=True)
        if not exc.delivered:
            raise typer.Exit(2) from exc
        _no_decision(request)
        raise typer.Exit(4) from exc
    typer.echo(json_mod.dumps(response, sort_keys=True))
    outcome = outcome_of(response)
    if outcome == UNKNOWN:
        _no_decision(request)
    raise typer.Exit({REFUSED: 2, REJECTED: 3, UNKNOWN: 4}.get(outcome, 0))


_SOCKET_OPT = typer.Option(
    ...,
    "--socket",
    "-S",
    help="The engine's control socket (<run_root>/control.sock).",
)


def _read_revision(socket_path: Path, key: str) -> tuple[str, int, int]:
    """The ss6 read header (`baseline_id`, `epoch`) and the current revision
    of `key` -- the read half of a read-then-write, for an operator who did
    not carry a revision in by hand.

    It narrows the race to one round trip; it does not remove it, and it
    cannot: the value of a precondition is that it names what the DECIDER
    saw, and a number this process fetched a millisecond ago is only a very
    recent guess about that. Whoever looked at a status page and then chose
    to act should pass --expect with the revision they looked at."""
    from dsl41.runner_control import read_for, revision_in

    response = _control_roundtrip(socket_path, read_for(key))
    baseline, epoch = response.get("baseline_id"), response.get("epoch")
    if not isinstance(baseline, str) or not isinstance(epoch, int):
        typer.echo(str(response.get("error", "the engine answered no read header")), err=True)
        raise typer.Exit(2)
    return baseline, epoch, revision_in(response, key)


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
    expect: int = typer.Option(
        None,
        "--expect",
        help="The state_rev you read for the target (from `query status`/`global`)."
        " The command is rejected if it moved since. Omitted, this reads it first --"
        " which narrows the race to one round trip, not to nothing. 0 means"
        " 'still absent' (SET_GLOBAL's conditional create).",
    ),
    request_id: str = typer.Option(
        None,
        "--request-id",
        help="RETRY the command that carried this id, rather than issuing a new one."
        " An exact retry -- same id, same envelope -- is answered from the original"
        " decision and applies nothing twice, which is the only safe response to"
        " exit 4. A fresh uuid4 otherwise.",
    ),
) -> None:
    """Vendor-parity sendevent against a running engine (runner-design ss10),
    over the v2 protocol (concurrency-model ss6).

    Every mutation names the revision it was composed against and is
    answered with its DECISION, in four kinds that call for four different
    next moves -- so they get four exit codes rather than one failure
    (control-protocol ss3):

      0  applied.
      2  REFUSED: nothing admitted, no index consumed, and the log says
         nothing about it. Fix it and send it again; unchanged is safe too.
      3  REJECTED: a decision went against it -- the target moved between
         the read and the write. It IS in the log. Re-read and re-decide;
         resending the same envelope loses the same race.
      4  UNKNOWN: no decision arrived. NOT a failure -- the command may be
         durably admitted and about to apply. Re-read; if it must be sent
         again, send it with --request-id and the id printed on stderr."""
    from dsl41.runner_admission import addressed_key
    from dsl41.runner_clock import EngineError
    from dsl41.runner_control import claimed_actor, command

    verb = event.upper()
    payload: dict = {}
    if job is not None:
        payload["job"] = job
    if status is not None:
        payload["status"] = status.upper()
    if global_kv is not None:
        name, sep, value = global_kv.partition("=")
        if not sep or not name:
            typer.echo('--global expects "NAME=value"', err=True)
            raise typer.Exit(2)
        payload["name"], payload["value"] = name, value
    if exit_code is not None:
        payload["exit_code"] = exit_code
    try:
        key = addressed_key(verb, payload)
    except EngineError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    baseline, epoch, current = _read_revision(socket_path, key)
    request = command(
        verb,
        payload,
        key=key,
        revision=current if expect is None else expect,
        baseline_id=baseline,
        epoch=epoch,
        request_id=request_id,
        claimed_actor=claimed_actor(),
    )
    _mutate(socket_path, request)


@app.command()
def host(
    action: str = typer.Argument(..., help="list|drain|activate|evict"),
    host_id: str = typer.Argument(None, help="The host id (all but `list`)."),
    socket_path: Path = _SOCKET_OPT,
    force: bool = typer.Option(
        False,
        "--force",
        help="evict: skip the ss8 preconditions. Recorded with the actor that"
        " claimed it, and the one path in the concurrency model that can produce"
        " a double run -- use it with out-of-band proof the machine is dead.",
    ),
    expect: int = typer.Option(
        None,
        "--expect",
        help="The state_rev you read for the host (from `host list`). The command"
        " is rejected if it moved since. Omitted, this reads it first.",
    ),
    request_id: str = typer.Option(
        None, "--request-id", help="RETRY the command that carried this id (see `sendevent`)."
    ),
) -> None:
    """The ss8 routing table: which execution hosts take new work
    (concurrency-model ss8).

      list      the table, with each host's revision.
      drain     stop routing NEW work here; running work finishes. Reversible,
                asserts nothing, and is the tool for planned maintenance.
      activate  route here again, and re-dispatch what the drain held.
      evict     declare this host's work rerouteable. The only state that lets
                another host run what was bound to this one, so it is refused
                unless the leader has recorded the host unreachable, the host
                runs a deadman, and the kill bound has passed.

    Mutations take `sendevent`'s four exit codes (0 applied / 2 refused /
    3 rejected / 4 unknown) for the same reason: four outcomes, four next
    moves."""
    import json as json_mod

    from dsl41.oracle_state import RuntimeState
    from dsl41.runner_control import claimed_actor, command

    verb = action.lower()
    if verb == "list":
        response = _control_roundtrip(socket_path, {"cmd": "hosts"})
        typer.echo(json_mod.dumps(response, sort_keys=True))
        raise typer.Exit(0 if response.get("ok") else 2)
    if not host_id:
        typer.echo(f"`host {verb}` needs a host id", err=True)
        raise typer.Exit(2)
    key = RuntimeState.host_key(host_id)
    baseline, epoch, current = _read_revision(socket_path, key)
    request = command(
        verb,
        {"id": host_id, "force": force},
        key=key,
        revision=current if expect is None else expect,
        baseline_id=baseline,
        epoch=epoch,
        request_id=request_id,
        claimed_actor=claimed_actor(),
        cmd="host",
    )
    _mutate(socket_path, request)


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


def _brief_flags(row: dict[str, object]) -> str:
    """The --brief flags column, I/H/N/A in the TUI's fixed order -- both
    surfaces render the same alphabet from the same status payload (DL-68)."""
    marks = (("I", "on_ice"), ("H", "on_hold"), ("N", "on_noexec"), ("A", "armed"))
    return "".join(mark for mark, key in marks if row.get(key))


@app.command()
def query(
    what: str = typer.Argument(
        ...,
        help="status|trace|explain|spec|deps|timers|plan|global|globals"
        "|subscribe|is-success|is-failed",
    ),
    socket_path: Path = _SOCKET_OPT,
    job: str = typer.Option(
        None, "--job", "-J", help="status: filter; explain/spec/deps/is-*: the job."
    ),
    name: list[str] = typer.Option(
        None, "--name", "-N", help="global/globals: the global(s) to read. Repeatable."
    ),
    since: int = typer.Option(None, "--since", help="trace/subscribe: only records after SEQ."),
    brief: bool = typer.Option(
        False,
        "--brief",
        help="status: one line per job (name, status, at, run, exit, flags, rev)"
        " instead of the JSON document -- the estate-scale skim (DL-66).",
    ),
) -> None:
    """Read-only control-plane queries (runner-design ss10); `subscribe`
    streams journal records as JSON lines until interrupted. The headless
    autorep analog -- the ss11 TUI consumes the same verbs. `is-success` /
    `is-failed` are scriptable predicates (DL-65): print the current status
    and exit 0 when it matches (SUCCESS; FAILURE or TERMINATED), 1 when it
    does not -- shell glue's `systemctl is-active` analog.

    `status` and `global`/`globals` are the reads a `sendevent --expect` is
    composed from (concurrency-model ss6): both publish the `state_rev` of a
    NAMED entity, and `global` answers an unset name at revision 0 rather
    than omitting it, because absence you cannot name is absence you cannot
    lock against."""
    import json as json_mod
    import socket as socket_mod

    verb = what.lower()
    known = (
        "status",
        "trace",
        "explain",
        "spec",
        "deps",
        "timers",
        "plan",
        "global",
        "globals",
        "subscribe",
    )
    predicates = {"is-success": ("SUCCESS",), "is-failed": ("FAILURE", "TERMINATED")}
    if verb not in known and verb not in predicates:
        typer.echo(f"unknown query {what!r} ({'|'.join([*known, *predicates])})", err=True)
        raise typer.Exit(2)
    if verb in predicates:
        if job is None:
            typer.echo(f"{verb} requires --job", err=True)
            raise typer.Exit(2)
        response = _control_roundtrip(socket_path, {"cmd": "status", "job": job})
        if not response.get("ok"):
            typer.echo(str(response.get("error", "status query failed")), err=True)
            raise typer.Exit(2)
        current = response["jobs"][job]["status"]
        typer.echo(current)
        raise typer.Exit(0 if current in predicates[verb] else 1)
    if brief and verb != "status":
        typer.echo("--brief applies to status only", err=True)
        raise typer.Exit(2)
    if verb in ("global", "globals") and not name:
        typer.echo(f"{verb} requires --name (repeat it for several)", err=True)
        raise typer.Exit(2)
    if verb == "global" and len(name or ()) > 1:
        typer.echo("global names one; use `globals` for several", err=True)
        raise typer.Exit(2)
    request: dict = {"cmd": verb}
    if job is not None:
        request["job"] = job
    if since is not None:
        request["since"] = since
    if name:
        # one verb per shape, as the server has them: `global` names one and
        # `globals` a list, and asking for one through the plural would make
        # a client that wants a single revision unwrap a map to find it
        request.update({"name": name[0]} if verb == "global" else {"names": list(name)})
    if verb != "subscribe":
        response = _control_roundtrip(socket_path, request)
        if brief and response.get("ok"):
            for job_name in sorted(response.get("jobs", {})):
                row = response["jobs"][job_name]
                flags = _brief_flags(row)
                exit_code = row.get("exit_code")
                typer.echo(
                    f"{job_name:<44} {row.get('status', ''):<10}"
                    f" {(row.get('status_at') or '-'):<26}"
                    f" run {row.get('run_number', 0):<4}"
                    f" exit {'-' if exit_code is None else exit_code:<4}"
                    # the revision goes on the skim because the skim is what
                    # an operator reads immediately before acting: --expect
                    # is only honest when it names what they LOOKED at
                    f" rev {row.get('state_rev', 0):<4} {flags}".rstrip()
                )
            if response.get("spec_drift"):
                typer.echo("SPEC DRIFT: estate files changed on disk", err=True)
            raise typer.Exit(0)
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

    from dsl41.runner_adapters import SupervisorConn

    verb = action.lower()
    if verb not in ("list", "shutdown"):
        typer.echo(f"unknown supervise action {action!r} (list|shutdown)", err=True)
        raise typer.Exit(2)
    sock_path = run_root / "supervisor.sock"
    if not sock_path.exists():
        typer.echo(f"no supervisor at {sock_path}", err=True)
        raise typer.Exit(2)
    try:
        conn = SupervisorConn(sock_path)
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
