"""Typer CLI entry points (pyproject: `dsl41 = dsl41.cli:app`).

This module is the ASSEMBLY: it builds the app, registers every verb in
the order `dsl41 --help` lists them, and owns nothing else. The verbs
themselves live one file per domain (DL-137's split), and each file says
what its domain is:

    cli_common.py   what more than one group needs: the shared options,
                    the catalog door, and the readings that turn an
                    exception or a control answer into an exit code
    cli_compile.py  a catalog in, an artifact out: lint, equiv, report,
                    uc, decompile, minify, folds, resolve, viz
    cli_run.py      run, rehearse, and their offline readers journal, runs
    cli_control.py  what an operator says to a RUNNING engine: sendevent,
                    host, query, ui, serve, supervise
    cli_estate.py   the period boundary and what lives around it: seal,
                    audit, verify, estate reclaim, estate prune

Imports run one way: this file imports the verb modules, the verb modules
import `cli_common`, and no library module imports any of them.

Exit-code contract (shared by all catalog-consuming commands): 0 success
(for lint: clean); 1 linter findings at or above the failing severity
(errors, or warnings too with --strict); 2 the input never reached the
tool (unreadable file, JIL parse error, placeholder-resolution failure,
or lowering refusal). `minify` extends it with 3: the input reached the
tool and the tool refused to emit (an unclassified attribute, a structural
mismatch, a surviving identifier). The runner verbs extend it further: see
cli_run.py's note for 1 vs 2, and DL-92's 2/3/4 for a mutation's four
outcomes.

Templated estates (DL-19/DL-22): every catalog-consuming command accepts
--properties/-p to resolve `~{$NAME}~` placeholders before parsing, so a
bunch of templated JILs lints/reports/derives as one catalog in one step.
Substitution is within-line, so diagnostics keep pointing at the real
file and line. The typed lanes (start_times etc.) stay strict on
unresolved tokens by design -- preprocessing IS the supported path.
"""

from __future__ import annotations

import typer

from dsl41 import cli_compile, cli_control, cli_estate, cli_run

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# Callback exists only to keep typer in subcommand mode -- without it,
# typer collapses a single @app.command() into a bare top-level command
# instead of a `dsl41 <verb> ...` subcommand.
@app.callback()
def _root() -> None:
    """Migration compiler and runner for AutoSys job estates.

    Compile JIL into reports, graphs and Stonebranch Universal Controller
    records. Run or rehearse an estate on this machine under AutoSys
    semantics, control it while it runs, and keep its history in sealed
    periods.

    Run 'dsl41 COMMAND --help' for one command's options and exit codes.
    """


# Panels and order are what `dsl41 --help` shows. Group by workflow, most
# common first.
app.command(rich_help_panel="Compile JIL")(cli_compile.lint)
app.command(rich_help_panel="Compile JIL")(cli_compile.report)
app.command(rich_help_panel="Compile JIL")(cli_compile.viz)
app.command(rich_help_panel="Compile JIL")(cli_compile.equiv)
app.command(rich_help_panel="Compile JIL")(cli_compile.uc)
app.command(rich_help_panel="Compile JIL")(cli_compile.decompile)
app.command(rich_help_panel="Compile JIL")(cli_compile.folds)
app.command(rich_help_panel="Compile JIL")(cli_compile.minify)
app.command(rich_help_panel="Compile JIL")(cli_compile.resolve)
app.command(rich_help_panel="Run an estate")(cli_run.run)
app.command(rich_help_panel="Run an estate")(cli_run.rehearse)
app.command(rich_help_panel="Run an estate")(cli_run.journal)
app.command(rich_help_panel="Run an estate")(cli_run.runs)
app.command(rich_help_panel="Control a running engine")(cli_control.query)
app.command(rich_help_panel="Control a running engine")(cli_control.sendevent)
app.command(rich_help_panel="Control a running engine")(cli_control.release_held)
app.command(rich_help_panel="Control a running engine")(cli_control.host)
app.command(rich_help_panel="Control a running engine")(cli_control.ui)
app.command(rich_help_panel="Control a running engine")(cli_control.serve)
app.command(rich_help_panel="Control a running engine")(cli_control.supervise)
app.command(rich_help_panel="Periods and retention")(cli_estate.seal)
app.command(rich_help_panel="Periods and retention")(cli_estate.audit)
app.command(rich_help_panel="Periods and retention")(cli_estate.verify)

estate_app = typer.Typer(
    no_args_is_help=True,
    help="Prune what retention allows, or reclaim a stale claim.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(estate_app, name="estate", rich_help_panel="Periods and retention")
estate_app.command("reclaim")(cli_estate.estate_reclaim)
estate_app.command("prune")(cli_estate.estate_prune)
