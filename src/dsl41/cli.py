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
from dsl41.boundary import PeriodSealed
from dsl41.ir import CatalogIR, LoweringError, lower_catalog
from dsl41.period import root_is_unused
from dsl41.lint import lint_catalog
from dsl41.placeholders import PlaceholderError, load_properties, substitute

if TYPE_CHECKING:  # type-only: equiv's runtime import stays deferred (below)
    from datetime import datetime

    from dsl41.equiv import TierAResult, TierBCatalogResult, TierCResult
    from dsl41.boundary import SealRequest
    from dsl41.runner import Engine
    from dsl41.runner_ledger import LeaderLock
    from dsl41.seal import StagedNextPeriod
    from dsl41.period import RuntimeProfile, StagedManifest
    from dsl41.runner_history import RunRow

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
        ...,
        help="Run journal to replay: an estate root, its journal.jsonl sentinel, or a"
        " wal/NNNNNN.jsonl segment. A root or a sentinel resolves to the ACTIVE"
        " segment (the current period); name an earlier wal/NNNNNN.jsonl to replay"
        " an earlier one.",
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
    from dsl41.oracle import Oracle
    from dsl41.oracle_state import OracleError
    from dsl41.period import catalog_hash_for, opening_at
    from dsl41.runner_clock import EngineError
    from dsl41.runner_hosts import LOCAL_EXECUTOR_ID, seed_local_executor
    from dsl41.runner_journal import read_journal, replay_inputs

    catalog = _load_catalog_or_exit_2(files, permit_unknown, properties)
    try:
        records = read_journal(journal_file)
    except (OSError, EngineError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    opening = records[0]
    # like for like (period-model ss1.1): the recipe is the one the
    # `segment` itself pins, never the one this build happens to write
    if opening.get("catalog_hash") != catalog_hash_for(opening, catalog):
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
    seed_local_executor(oracle.store, LOCAL_EXECUTOR_ID, at=opening_at(opening))
    try:
        replay_inputs(oracle, records)
    except OracleError as exc:
        typer.echo(f"replay failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    for entry in oracle.trace():
        typer.echo(f"{entry.at.isoformat()} {entry.job} {entry.transition} [{entry.cause}]")


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


@app.command()
def runs(
    run_roots: list[Path] = typer.Argument(
        ..., help="One or more run roots (dsl41 run --run-root TARGET)."
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
    line -- never silently, and never refused."""
    from datetime import UTC
    from datetime import datetime as datetime_mod

    from dsl41.runner_history import RunHistoryError, read_run_roots

    since_at = None
    if since is not None:
        try:
            since_at = datetime_mod.fromisoformat(since)
        except ValueError as exc:
            typer.echo(f"--since: {exc}", err=True)
            raise typer.Exit(2) from exc
        if since_at.tzinfo is not None:  # journal timestamps are naive UTC
            since_at = since_at.astimezone(UTC).replace(tzinfo=None)
    try:
        rows = read_run_roots(run_roots, job=job, since=since_at)
    except RunHistoryError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    if any(row.fidelity == "records_only" for row in rows):
        # Loud on stderr as well as on the row, because the one degraded
        # field that MISLEADS rather than omits is `status`.
        typer.echo(
            "warning: some run roots have no stored inputs, so their rows are"
            " fidelity=records_only: no box rows, no box_name/started_by/job_hash,"
            " and a run closed by KILLJOB or term_run_time reads as RUNNING",
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
        buf = io.StringIO()
        writer = csv_mod.writer(buf)
        writer.writerow(fields)
        for row in rows:
            dump = row.model_dump(mode="json")
            writer.writerow(["" if dump[f] is None else dump[f] for f in fields])
        typer.echo(buf.getvalue(), nl=False)
    else:
        for line in _runs_table(rows):
            typer.echo(line)
    raise typer.Exit(0)


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
    if open_from is not None:
        if resume:
            typer.echo(
                "--open-from and --resume are the two OPENERS and you get one:"
                " --resume continues the lineage in this root, --open-from opens the"
                " next period into a fresh one (period-model ss7)",
                err=True,
            )
            raise typer.Exit(2)
        if estate_anchor is not None and Path(estate_anchor) != Path(open_from):
            typer.echo(
                f"--open-from {open_from} IS the lineage anchor; --estate-anchor"
                f" {estate_anchor} names another one (period-model ss7)",
                err=True,
            )
            raise typer.Exit(2)
        estate_anchor = open_from
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
    from dsl41.period import RuntimeProfile, runtime_hash, to_us

    if staged is None:
        return None
    observed = None if running_deadman is None else to_us(running_deadman)
    if observed == staged.runtime_profile.deadman_us:
        return staged
    profile = RuntimeProfile.model_validate(
        {**staged.runtime_profile.model_dump(), "deadman_us": observed}
    )
    return staged.model_copy(
        update={"runtime_profile": profile, "runtime_hash": runtime_hash(profile)}
    )


def _active_period(run_root: Path) -> int:
    """Which period this root's ACTIVE segment holds (period-model I1).

    1 on a root that has never sealed. A reader that
    defaulted to 1 after a boundary would read period 1's manifest beside
    period N's records -- and a ROLLED root has no period 1 at all."""
    from dsl41.runner_history import RunHistoryError, active_period_id

    try:
        return active_period_id(run_root)
    except RunHistoryError:
        from dsl41.period import GENESIS_PERIOD_ID

        return GENESIS_PERIOD_ID


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
    from dsl41.period import RuntimeProfile, read_period_manifest, runtime_hash, to_us
    from dsl41.runner_clock import EngineError

    try:
        # the ACTIVE period's manifest, never period 1's: every artifact
        # under `periods/` is addressed by the period number, and a rolled
        # root holds only the period it was opened into (DL-134)
        manifest = read_period_manifest(run_root, _active_period(run_root))
    except EngineError as exc:
        return str(exc)
    if manifest is None:
        return None
    observed_deadman = None if running_deadman is None else to_us(running_deadman)
    observed = RuntimeProfile.model_validate(
        {**profile.model_dump(), "deadman_us": observed_deadman}
    )
    if runtime_hash(observed) == manifest.runtime_hash:
        return None
    pinned = manifest.runtime_profile
    moved = sorted(
        name
        for name in type(observed).model_fields
        if getattr(observed, name) != getattr(pinned, name)
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

    from datetime import datetime

    from dsl41.boundary import stage_period
    from dsl41.period import RuntimeProfile, to_us
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
            typer.echo(str(exc), err=True)
            return 2
    # ACQUIRE first (S6a, concurrency-model ss7). Earlier than the engine's
    # own entry points would, because the next thing this function does is
    # START a supervisor and take its lease -- an act on an estate this
    # process may turn out not to lead.
    try:
        lock = acquire_run_root(run_root)
    except EngineError as exc:
        typer.echo(str(exc), err=True)
        return 2
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
            typer.echo(str(exc), err=True)
            lock.release()
            return 2
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
            typer.echo(str(exc), err=True)
            lock.release()
            return 2
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
            pinned = read_period_manifest(run_root, _active_period(run_root))
        except EngineError as exc:
            typer.echo(str(exc), err=True)
            return 2
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
            RuntimeProfile.model_validate(
                {**profile.model_dump(), "deadman_us": pinned_deadman_us}
            ),
        )
    except EngineError as exc:
        typer.echo(str(exc), err=True)
        return 2
    client = wiring.client
    adapters = wiring.adapters
    scheduler = wiring.scheduler
    running_deadman = _running_deadman(client, deadman, run_root)
    staged = _observed_profile(staged, running_deadman)
    if resume:
        error = _resume_profile_error(run_root, profile, running_deadman)
        if error is not None:
            typer.echo(error, err=True)
            return 2
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
        _say_next(run_root, anchor_dir)
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

    from dsl41.boundary import stage_period
    from dsl41.oracle_state import Event, OracleError
    from dsl41.period import runtime_profile_from_cli
    from dsl41.runner import Engine
    from dsl41.runner_startup import start_run
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import EngineError, VirtualClock
    from dsl41.runner_scheduler import Scheduler

    catalog, parsed, _ = _load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)
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
    header = _read_header_of(response)
    if header is None:
        raise typer.Exit(2)
    baseline, epoch = header
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
    from dsl41.runner_control import versioned

    # the raw socket is a client like `roundtrip` is: an unversioned
    # subscribe is refused, and a refusal does not close the connection, so
    # without the stamp this loop prints the refusal and waits forever
    request = versioned(request)
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


# ------------------------------------------------------- the boundary (U7)

_RUN_ROOT_OPT = typer.Option(..., "--run-root", help="The estate root (period-model ss1.1).")

_ANCHOR_OPT = typer.Option(
    None,
    "--estate-anchor",
    help="The lineage anchor directory (period-model ss1.3). Defaults to"
    " <run-root>.anchor -- a sibling of the root, never inside it, because the"
    " root is what an operator archives. A ROLLED root's anchor is the lineage's"
    " and must be named explicitly.",
)

_ACTOR_OPT = typer.Option(
    None,
    "--claimed-actor",
    help="Who is asking, for the log. A CLAIM: this tier has no authentication"
    " (control-protocol ss7 gap 2), so it is a breadcrumb, never an"
    " authorization. Defaults to <user>@<host>.",
)


def _next_profile(
    timezone: "str | None",
    timezone_map: "Path | None",
    as_machine: list[str],
    machine_policy: str,
    detached: bool,
    deadman: "float | None",
) -> "RuntimeProfile":
    """C2's `RuntimeProfile` from the `--next-*` flags (period-model ss2.1).

    Prefixed because a boundary names TWO periods and the CLI would
    otherwise read as if it were describing the one that is running. What
    it describes is the one about to open."""
    from dsl41.period import runtime_profile_from_cli

    if machine_policy not in ("strict", "local-eligible"):
        # the same guard `run`/`rehearse` apply: without it a bad flag
        # surfaces as an uncaught ValidationError and exit 1 -- documented
        # as "the estate failed while running", which it never did (DL-137)
        typer.echo(f"--machine-policy {machine_policy!r}: expected strict|local-eligible", err=True)
        raise typer.Exit(2)
    tz_aliases = _load_tz_aliases(timezone_map)
    _check_base_tz(timezone, tz_aliases)
    return runtime_profile_from_cli(
        timezone=timezone,
        tz_aliases=tz_aliases,
        as_machine=as_machine,
        machine_policy=machine_policy,
        detached=detached,
        deadman_s=deadman,
    )


def _stage_next(
    run_root: Path,
    files: list[Path],
    profile: "RuntimeProfile",
    permit_unknown: bool,
    properties: "list[Path] | None",
) -> "tuple[StagedNextPeriod, StagedManifest, CatalogIR]":
    """ss7's staging, both modes: the immutable bundle, then
    `staged_manifest.json` and `candidate.json` under
    `periods/.staging/<stage_digest>/`.

    Content-addressed, so a repeat is idempotent and a concurrent client
    writing the same bytes is harmless -- which is what makes it safe to do
    this against a LIVE engine's root without holding its lock."""
    from dsl41.boundary import stage_next_period, stage_period

    catalog, parsed, _ = _load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)
    staged_manifest = stage_period(run_root, parsed, catalog, profile)
    staged = stage_next_period(run_root, staged_manifest=staged_manifest)
    return staged, staged_manifest, catalog


def _read_header_of(response: dict) -> "tuple[str, int] | None":
    """The ss6 read header off one control answer, or None after printing
    the refusal -- the ONE check (DL-137): two verbatim copies differed
    only in how they exited."""
    baseline, epoch = response.get("baseline_id"), response.get("epoch")
    if not isinstance(baseline, str) or not isinstance(epoch, int):
        typer.echo(str(response.get("error", "the engine answered no read header")), err=True)
        return None
    return baseline, epoch


@app.command()
def seal(
    next_files: list[Path] = typer.Option(
        ...,
        "--next",
        help="JIL file(s) forming C2 -- the estate the NEXT period runs."
        " Repeatable; command-line order is part of source_bundle_hash.",
    ),
    run_root: Path = _RUN_ROOT_OPT,
    estate_anchor: Path = _ANCHOR_OPT,
    force_seal: bool = typer.Option(
        False,
        "--force-seal",
        help="Commit inside the closing period's retry horizon (period-model ss9)."
        " Recorded as force_seal: true in the seal and, when the gate was really"
        " engaged, with the gate's own numbers in forced_gate.",
    ),
    claimed_actor: str = _ACTOR_OPT,
    request_id: str = typer.Option(
        None,
        "--request-id",
        help="Reuse the id of an earlier attempt to RETRY it exactly. A committed"
        " boundary answers its own exact retry from the next period; anything"
        " else is a fresh request.",
    ),
    next_timezone: str = typer.Option(
        None, "--next-timezone", help="C2's base zone for schedules without a per-job one."
    ),
    next_timezone_map: Path = typer.Option(
        None, "--next-timezone-map", help="C2's vendor timezone table (SEM-35/DL-62)."
    ),
    next_as_machine: list[str] = typer.Option(
        [], "--next-as-machine", help="Machine name(s) C2 runs as (DL-52). Repeatable."
    ),
    next_machine_policy: str = typer.Option(
        "strict", "--next-machine-policy", help="C2's machine policy: strict|local-eligible."
    ),
    next_detached: bool = typer.Option(
        False, "--next-detached", help="C2 runs CMD jobs under the supervisor (ss6a Tier 1)."
    ),
    next_deadman: float = typer.Option(
        None, "--next-deadman", help="C2's supervisor deadman, seconds. Needs --next-detached."
    ),
    permit_unknown: bool = _PERMIT_UNKNOWN,
    properties: list[Path] = _PROPERTIES,
) -> None:
    """Close the running period and commit the next one (period-model ss7).

    Two entry modes and one body. **Live**: an engine leads `--run-root`,
    so this stages C2 and asks it over the control socket; the engine runs
    the cutoff in its single-writer loop and then exits with code 3.
    **Offline**: nothing leads the root, so this takes `leader.lock` and
    `anchor.lock`, appends a `leader` record, runs the same-root recovery
    barrier in full, and performs the boundary as that offline leader.
    Which one you get is decided by the lock, not by a flag: an engine that
    holds it is a live engine.

    Step 9 in both modes is an OPENER -- `dsl41 run --resume` on the same
    root, or `dsl41 run --open-from` into a fresh one. A transition is a
    restart, not a reload.

    Exit codes: 0 the boundary committed; 2 it did NOT commit and the period
    is still open (C1 may legitimately have advanced first -- an offline
    sealer's `leader` record and the cutoff's admitted ticks are C1
    activity, not damage); 4 the outcome is UNKNOWN, and the printed
    request_id is the only safe way to retry.
    """
    import asyncio

    from dsl41.runner_clock import EngineError
    from dsl41.runner_control import claimed_actor as default_actor
    from dsl41.runner_ledger import acquire_run_root

    if next_deadman is not None and not next_detached:
        typer.echo(
            "--next-deadman needs --next-detached: a tethered run has no supervisor", err=True
        )
        raise typer.Exit(2)
    profile = _next_profile(
        next_timezone,
        next_timezone_map,
        next_as_machine,
        next_machine_policy,
        next_detached,
        next_deadman,
    )
    actor = claimed_actor or default_actor()
    try:
        lock = acquire_run_root(run_root)
    except EngineError:
        # the lock IS the discriminator: an engine that holds it is a live
        # engine, and probing a socket would answer a different question
        # (a socket file outlives the process that made it)
        raise typer.Exit(
            _live_seal(
                run_root,
                next_files,
                profile,
                permit_unknown,
                properties,
                force_seal,
                actor,
                request_id,
            )
        ) from None
    try:
        raise typer.Exit(
            asyncio.run(
                _offline_seal(
                    run_root,
                    estate_anchor,
                    next_files,
                    profile,
                    permit_unknown,
                    properties,
                    force_seal,
                    actor,
                    request_id,
                    lock,
                )
            )
        )
    finally:
        lock.release()


def _live_seal(
    run_root: Path,
    next_files: list[Path],
    profile: "RuntimeProfile",
    permit_unknown: bool,
    properties: "list[Path] | None",
    force_seal: bool,
    actor: str,
    request_id: "str | None",
) -> int:
    """ss7 live mode: stage C2, then ask the leading engine for the
    boundary over the control socket.

    The CLI stages FIRST and names the staged bytes by `stage_digest`; the
    engine validates exactly those bytes. Two clients racing on one root
    stage under two fingerprints and the engine commits exactly the one its
    request names."""
    import json as json_mod
    import uuid

    from dsl41.runner_control import ControlClientError, roundtrip

    socket_path = run_root / "control.sock"
    if not socket_path.exists():
        typer.echo(
            f"{run_root}: an engine holds leader.lock and {socket_path} is not there --"
            " a live seal is asked for over the control socket, and this root has a"
            " leader with no door (period-model ss7)",
            err=True,
        )
        return 2
    try:
        header = roundtrip(socket_path, {"cmd": "status"})
    except ControlClientError as exc:
        typer.echo(f"{socket_path}: {exc}", err=True)
        return 2
    parsed_header = _read_header_of(header)
    if parsed_header is None:
        return 2
    baseline, epoch = parsed_header
    # `staged` is the OWNER's projection (stage_next_period's return,
    # DL-137) -- the reflection rebuild it replaces was the third spelling
    # of which fields cross from launcher-pin to client-proposal
    staged, staged_manifest, _ = _stage_next(
        run_root, next_files, profile, permit_unknown, properties
    )
    request = {
        "cmd": "seal",
        "v": 3,
        "baseline_id": baseline,
        "epoch": epoch,
        "request_id": request_id or str(uuid.uuid4()),
        "next_period": staged.model_dump(mode="json"),
        "stage_digest": staged.stage_digest,
        "force_seal": force_seal,
        "claimed_actor": actor,
    }
    try:
        answer = roundtrip(socket_path, request)
    except ControlClientError as exc:
        typer.echo(str(exc), err=True)
        if not exc.delivered:
            return 2
        _no_decision(request)
        return 4
    typer.echo(json_mod.dumps(answer, sort_keys=True))
    if answer.get("ok"):
        _say_next(run_root, None)
        return 0
    if answer.get("refused"):
        return 2
    _no_decision(request)
    return 4


async def _offline_seal(
    run_root: Path,
    estate_anchor: "Path | None",
    next_files: list[Path],
    profile: "RuntimeProfile",
    permit_unknown: bool,
    properties: "list[Path] | None",
    force_seal: bool,
    actor: str,
    request_id: "str | None",
    lock: "LeaderLock",
) -> int:
    """ss7 offline mode: no engine, so this process becomes the leader for
    exactly one boundary.

    `leader.lock` is already held (the caller took it, which is how the two
    modes are told apart). `resume_run` is the same-root recovery barrier
    in full -- it takes `anchor.lock`, appends a `leader` record at
    epoch+1, replays, reconciles and re-drives recorded kills -- and the
    boundary that follows is the SAME `submit_seal` a live engine serves.
    Two entry modes, one body; the alternative is two implementations of
    the one thing this model exists to have exactly one of.

    C1 is loaded from the ROOT's own bundle, never from the command line:
    the run root outlives the estate files it was launched from, and the
    closing period's identity is the manifest's."""
    import uuid

    from dsl41.boundary import SealRequest
    from dsl41.runner_clock import EngineError, RealClock
    from dsl41.runner_history import active_period_id
    from dsl41.period import read_period_manifest
    from dsl41.runner_startup import resume_run, wire_from_profile

    try:
        pinned = read_period_manifest(run_root, active_period_id(run_root))
    except Exception as exc:  # RunHistoryError or EngineError: both are refusals
        typer.echo(f"{run_root}: {exc}", err=True)
        return 2
    if pinned is None:
        typer.echo(
            f"{run_root}: no period manifest -- an offline seal reads the CLOSING"
            " period's identity from the root itself, and this root has none"
            " (period-model ss7)",
            err=True,
        )
        return 2
    staged, staged_manifest, _ = _stage_next(
        run_root, next_files, profile, permit_unknown, properties
    )
    wiring = None
    try:
        catalog = _catalog_from_root(run_root, pinned.source_bundle_hash)
        wiring = await wire_from_profile(run_root, catalog, pinned.runtime_profile)
        engine = await resume_run(
            catalog,
            run_root,
            clock=RealClock(),
            adapters=wiring.adapters,
            scheduler=wiring.scheduler,
            hold_open=True,
            supervisor=wiring.client,
            deadman_s=wiring.deadman_s,
            lock=lock,
            anchor_dir=estate_anchor,
        )
    except EngineError as exc:
        typer.echo(str(exc), err=True)
        if wiring is not None:
            await wiring.close()
        return 2
    request = SealRequest(
        baseline_id=engine.baseline_id,
        epoch=engine.epoch,
        request_id=request_id or str(uuid.uuid4()),
        next_period=staged,
        stage_digest=staged.stage_digest,
        force_seal=force_seal,
        claimed_actor=actor,
    )
    code = await _drive_boundary(engine, request, run_root, estate_anchor)
    await wiring.close()
    return code


async def _drive_boundary(
    engine: "Engine", request: "SealRequest", run_root: Path, estate_anchor: "Path | None"
) -> int:
    """One queued boundary, driven to its outcome by this process's own
    loop -- the offline sealer's tail.

    Two things can finish first and they mean different things. The LOOP
    ending is a committed boundary (`PeriodSealed`), a fail-stop, or a
    crash. The REQUEST's future ending while the loop runs on is a refusal:
    `abort_boundary` reopened admission and the period is still open, which
    is exactly what ss7's exit code 2 is for -- so waiting on the loop
    alone would wait forever for an engine that is correctly still
    serving C1.

    The three exits are ss7's: 0 committed, 2 refused with C1 still open,
    4 an unknown outcome whose only safe retry is the printed
    `request_id`."""
    import asyncio
    import contextlib

    from datetime import datetime

    from dsl41.boundary import BoundaryFailStop, PeriodSealed
    from dsl41.runner_clock import EngineError

    # `ensure_future` over the engine's own future: it hands the same
    # object back and gives this function the `done`/`exception`/`result`
    # surface its three-way classification is written against
    future = asyncio.ensure_future(engine.submit_seal(request))
    loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    await asyncio.wait({future, loop_task}, return_when=asyncio.FIRST_COMPLETED)
    code = 2
    outcome: BaseException | None = None
    if future.done() and future.exception() is None:
        # **The FUTURE decides, not the loop.** It is the boundary's own
        # answer, and once it holds a committed boundary the boundary
        # committed -- whatever the loop does afterwards. Reading the loop
        # first would let an unrelated engine failure during teardown
        # report exit 2, which ss7 defines as "it did NOT commit and the
        # period is still open": the one lie about the estate this
        # function could tell. The loop is still awaited, because it is a
        # bounded number of turns from its own `PeriodSealed` and that
        # object is the single authority for the sentence an operator
        # reads; anything else it raises is printed as diagnostics.
        outcome = PeriodSealed(future.result())
        try:
            await loop_task
        except PeriodSealed as sealed:
            outcome = sealed
        except BaseException as raised:  # noqa: BLE001 -- diagnostics only
            typer.echo(f"the engine stopped after the boundary: {raised}", err=True)
    elif loop_task.done():
        outcome = loop_task.exception()
    else:
        outcome = future.exception()
        loop_task.cancel()
    if isinstance(outcome, PeriodSealed):
        typer.echo(str(outcome))
        _say_next(run_root, estate_anchor)
        code = 0
    elif isinstance(outcome, BoundaryFailStop):
        typer.echo(str(outcome), err=True)
        code = 4
    elif outcome is not None:
        typer.echo(str(outcome), err=True)
        code = 2 if isinstance(outcome, EngineError) else 1
    else:
        typer.echo("the engine loop returned without a boundary", err=True)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await loop_task
    with contextlib.suppress(Exception):
        await engine.shutdown()
    if engine.journal is not None:
        engine.journal.close()
    return code


def _say_next(run_root: Path, estate_anchor: "Path | None") -> None:
    anchor = f" --estate-anchor {estate_anchor}" if estate_anchor is not None else ""
    typer.echo(
        f"open it with `dsl41 run --resume --run-root {run_root}{anchor} <new estate files>`,"
        f" or roll: `dsl41 audit --run-root {run_root}{anchor}` then"
        " `dsl41 run --open-from <anchor-dir> --run-root <new-root> <new estate files>`"
    )


def _catalog_from_root(run_root: Path, source_bundle_hash: str) -> "CatalogIR":
    """C1, from the root's own immutable bundle (period-model ss7).

    Parsed under the ORIGINAL paths `sources.json` records, because
    `catalog_hash` v2 covers spans and a span names its file."""
    from dsl41.period import bundle_sources

    sources = bundle_sources(run_root, source_bundle_hash)
    return lower_catalog([parse(source.text, file=source.path) for source in sources])


@app.command()
def audit(
    run_root: Path = _RUN_ROOT_OPT,
    estate_anchor: Path = _ANCHOR_OPT,
    period: int = typer.Option(
        None, "--period", help="Audit exactly this period. Omit to audit every closed one."
    ),
) -> None:
    """Re-derive a closed period and write its attestation (period-model
    ss1.3, ss11).

    **Verified means re-derived, not self-consistent.** A sidecar whose
    digest matches its own canonical form proves integrity, not derivation,
    so this rebuilds the seal from the period's own evidence -- the opening
    seal, the complete ordered WAL, the immutable spool, and the C1 and C2
    manifests -- and refuses when the two disagree, naming the fields.

    **Producing an attestation and consuming one are two acts with two
    rules.** Producing N requires the PREDECESSOR attestation present and
    VERIFIED;
    period 1 is the base case. There is deliberately no "or re-derive
    everything below" alternative, because without the requirement a
    checkpoint can be emitted over an unaudited opening seal and earlier
    roots then get deleted on a chain that was never established.

    `dsl41 verify` is the other verb and is not this one: it validates an
    attestation, which is what a rolled root can do and a full audit is
    not.

    Exit 0 when every period asked for is attested, 2 on any refusal.
    """
    from dsl41.attest import Unattested, audit_period
    from dsl41.boundary import EstateAnchor, default_anchor_dir
    from dsl41.period import closed_periods
    from dsl41.runner_clock import EngineError

    periods = [period] if period is not None else closed_periods(run_root)
    if not periods:
        typer.echo(f"{run_root}: no closed period to audit", err=True)
        raise typer.Exit(2)
    # the anchor is taken by `audit_period` for the ONE write that needs it
    # and released again, so auditing a closed period while a later one is
    # live is possible: a leader holds the lineage lock for its whole life
    anchor = EstateAnchor(estate_anchor or default_anchor_dir(run_root))
    try:
        for period_id in periods:
            attestation = audit_period(run_root, period_id, anchor=anchor)
            typer.echo(
                f"period {period_id} attested: {attestation.digest}"
                f" (seal {attestation.seal_digest}, chain through"
                f" {attestation.chain_through_period})"
            )
    except Unattested as exc:
        # the checkpoint IS written; only the registry row is not, and the
        # row is bookkeeping. Loud on stderr, and not a failure
        typer.echo(str(exc), err=True)
    except EngineError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@app.command()
def verify(
    run_root: Path = _RUN_ROOT_OPT,
    period: int = typer.Option(
        None, "--period", help="Verify this period's attestation. Omit for the newest one."
    ),
) -> None:
    """Validate an attestation: its own digest, its binding to the seal it
    names, and its place in the chain (period-model ss1.3).

    It accepts the attestation ALONE, deliberately. The producing `audit`
    already established the induction, and a physical roll imports only the
    current seal and its attestation -- a consumer that re-walked the chain
    would make a second roll impossible. So a root that imported seal 2 and
    attestation 2 while its predecessors are gone verifies the chain below
    seal 2, because attestation 2 proves it.

    Exit 0 when it verifies, 2 otherwise.
    """
    from dsl41.attest import verify_attestation
    from dsl41.period import attestation_path
    from dsl41.runner_clock import EngineError

    if period is None:
        held = sorted(
            int(entry.name.split(".")[0])
            for entry in (run_root / "seals").glob("*.audit.json")
            if entry.name.split(".")[0].isdigit()
        )
        if not held:
            typer.echo(f"{run_root}: no attestation to verify", err=True)
            raise typer.Exit(2)
        period = held[-1]
    try:
        attestation = verify_attestation(run_root, period)
    except EngineError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(
        f"{attestation_path(run_root, period)} verifies: seal {attestation.seal_digest},"
        f" chain through period {attestation.chain_through_period},"
        f" produced by dsl41 {attestation.dsl41_version}"
    )


estate_app = typer.Typer(
    no_args_is_help=True,
    help="Lineage-level operations: prune what retention allows, and the"
    " break-glass reclaim.",
)
app.add_typer(estate_app, name="estate")


@estate_app.command("reclaim")
def estate_reclaim(
    estate_anchor: Path = typer.Option(
        ..., "--estate-anchor", help="The lineage anchor directory (period-model ss1.3)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Required. This is the one operation that can fork a lineage."
    ),
    claimed_actor: str = _ACTOR_OPT,
) -> None:
    """Break-glass: move a successor claim out of the way (period-model
    ss1.3).

    **A stale claim is break-glass, not garbage.** A `claimed` head whose
    target root is unreachable cannot be told from one whose target is
    merely paused, and nothing here decides that -- you do. If the claimant
    is alive, this forks the lineage: two roots then open the same period,
    allocate the same indices and run the same `(job, run_number)` twice,
    which is the safety property the whole fence exists to hold. Prove the
    claimant is gone before you run it.

    It is recorded in the anchor and again in the next `segment` record's
    `reclaimed` field with the actor who claimed to authorize it -- loud,
    durable and attributable.

    Exit 0 when the head moved, 2 otherwise.
    """
    from dsl41.boundary import EstateAnchor
    from dsl41.runner_clock import EngineError
    from dsl41.runner_control import claimed_actor as default_actor

    if not force:
        typer.echo(
            "refusing without --force: reclaiming a live claimant's head forks the"
            " lineage, and this verb exists for the case where you have PROVED it is"
            " gone (period-model ss1.3)",
            err=True,
        )
        raise typer.Exit(2)
    anchor = EstateAnchor(estate_anchor)
    try:
        anchor.acquire()
    except EngineError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    try:
        stored = anchor.require()
        _, moved = anchor.reclaim(
            estate_id=stored.estate_id, claimed_actor=claimed_actor or default_actor()
        )
    except EngineError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    finally:
        anchor.release()
    typer.echo(
        f"reclaimed claim {moved.claim_id} from {moved.target_root}: period"
        f" {moved.next_period} may be opened again, and the next opening `segment`"
        f" will record that {moved.claimed_actor} said so"
    )


@estate_app.command("prune")
def estate_prune(
    run_root: Path = _RUN_ROOT_OPT,
    estate_anchor: Path = _ANCHOR_OPT,
    dry_run: bool = typer.Option(False, "--dry-run", help="List every verdict and delete nothing."),
    tombstones: bool = typer.Option(
        False,
        "--tombstones",
        help="Remove SPAWN tombstones -- run directory, `.by_run_id` entry and"
        " default logs -- whose period is attested and whose run is terminal.",
    ),
    quarantine: bool = typer.Option(
        False,
        "--quarantine",
        help="Remove quarantined candidates: superseded staged periods no recovery references.",
    ),
    older_than_days: float = typer.Option(
        None,
        "--older-than-days",
        help="Keep any run spool touched more recently than this. Your policy, not the model's.",
    ),
    keep_runs: int = typer.Option(
        0,
        "--keep-runs",
        help="Keep the N newest run spools OF EACH JOB, whatever else says."
        " Per job, because `run_number` is per job.",
    ),
) -> None:
    """Delete what retention allows, and report what it does not
    (period-model ss11a, ss12; PR-36b, PR-36c).

    **Retention policy is yours; the floors are the model's.** Which
    periods, spools and tombstones an estate keeps is a business decision,
    so the flags above are how you state it. What may never go is
    everything reachable from the lineage head -- the sentinel, the anchor
    and any live claim, the sidecars this period opened from and will close
    with, the current and committed-next manifests, an uncommitted
    candidate's two files, their bundles, the latest attestation, and the
    WAL and spool of any unattested period. This verb cannot reach them.

    Three verdicts are reported. `floored` is refused by the model.
    `held` has been released by the head moving on and is kept anyway,
    because PR-Q3/E20 -- may a seal-only archive stand in for pruned
    inputs? -- is open. `prunable` is licensed by name: a tombstone whose
    period is attested and whose run has ended, and a quarantined
    candidate.

    Pruning a tombstone is not reversible and it is not free: that period
    can no longer be re-derived from its own evidence, and its attestation
    becomes the proof that stands for it. Attest first, then prune.

    Exit 0 when every selected artifact was removed (or listed, under
    `--dry-run`), 2 on a refusal and 2 when the filesystem refused a
    removal -- and then the report says which ones went and which did not.
    """
    from dsl41.retention import CLASSES, Artifact, plan_retention, prune
    from dsl41.runner_clock import EngineError

    classes = [name for name, on in (("tombstones", tombstones), ("quarantine", quarantine)) if on]
    if not classes and dry_run:
        # a listing with no class named is a survey: it shows every
        # licensed deletion, so the operator can pick from what is there
        classes = sorted(CLASSES)
    if not classes and not dry_run:
        typer.echo(
            "nothing selected: name at least one class (--tombstones, --quarantine)"
            " or ask for --dry-run. A prune verb with a default set would be a"
            " retention policy, and that is the operator's (period-model ss12)",
            err=True,
        )
        raise typer.Exit(2)
    try:
        plan = plan_retention(run_root, anchor_dir=estate_anchor)
        report = prune(
            plan,
            classes=classes,
            dry_run=dry_run,
            older_than_days=older_than_days,
            keep_runs=keep_runs,
        )
    except EngineError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    def _lines(title: str, items: tuple[Artifact, ...]) -> None:
        typer.echo(f"{title} ({len(items)}):")
        for item in items:
            typer.echo(f"  {item.render()}")

    verb = "would remove" if report.dry_run else "removed"
    _lines(verb, report.removed)
    _lines("prunable, outside the flags given", report.kept)
    _lines("held (floor lifted, PR-Q3/E20 open)", report.held)
    _lines("floored (the model refuses)", report.floored)
    if report.failed:
        typer.echo(f"the filesystem refused ({len(report.failed)}):", err=True)
        for item, reason in report.failed:
            typer.echo(f"  {item.path}: {reason}", err=True)
    typer.echo(
        f"{verb} {len(report.removed)} artifact(s), {report.bytes_removed} byte(s);"
        f" {len(report.floored)} floored, {len(report.held)} held"
        f" -- estate {plan.estate_id}, period {plan.current_period},"
        f" attested {sorted(plan.attested) or 'none'}"
    )
    if report.failed:
        raise typer.Exit(2)
