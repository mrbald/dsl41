"""Typer CLI entry points (pyproject: `dsl41 = dsl41.cli:app`).

This module is the ASSEMBLY: it builds the app, registers every verb in
the order `dsl41 --help` lists them, and owns nothing else. The verbs
themselves live one file per domain (DL-137's split), and each file says
what its domain is:

    cli_common.py   what more than one group needs: the shared options,
                    the catalog door, and the readings that turn an
                    exception or a control answer into an exit code
    cli_compile.py  a catalog in, an artifact out: lint, equiv, report,
                    uc, decompile, folds, resolve, viz
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
or lowering refusal). The runner verbs extend it: see cli_run.py's note
for 1 vs 2, and DL-92's 2/3/4 for a mutation's four outcomes.

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


# The registration order IS the order `dsl41 --help` lists the verbs in,
# so it is the order they were declared in before the split.
app.command()(cli_compile.lint)
app.command()(cli_compile.equiv)
app.command()(cli_compile.report)
app.command()(cli_compile.uc)
app.command()(cli_compile.decompile)
app.command()(cli_run.journal)
app.command()(cli_run.runs)
app.command()(cli_compile.folds)
app.command()(cli_compile.resolve)
app.command()(cli_compile.viz)
app.command()(cli_run.run)
app.command()(cli_run.rehearse)
app.command()(cli_control.sendevent)
app.command()(cli_control.host)
app.command()(cli_control.ui)
app.command()(cli_control.serve)
app.command()(cli_control.query)
app.command()(cli_control.supervise)
app.command()(cli_estate.seal)
app.command()(cli_estate.audit)
app.command()(cli_estate.verify)

estate_app = typer.Typer(
    no_args_is_help=True,
    help="Lineage-level operations: prune what retention allows, and the break-glass reclaim.",
)
app.add_typer(estate_app, name="estate")
estate_app.command("reclaim")(cli_estate.estate_reclaim)
estate_app.command("prune")(cli_estate.estate_prune)
