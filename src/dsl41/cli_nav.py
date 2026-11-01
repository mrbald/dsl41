"""Terminal and in-memory entry points for read-only estate navigation."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import typer

from dsl41.cli_common import PERMIT_UNKNOWN, PROPERTIES, refuse
from dsl41.cli_compile import load_navigation_catalog
from dsl41.derive import derive_graph
from dsl41.dsl import CatalogBuilder, DslError, job_to_source
from dsl41.ir import CatalogIR, LoweringError
from dsl41.ast_jil import JilParseError
from dsl41.nav import NavNotFound, navigate, validate_query


def _emit_navigation(
    load: Callable[[], CatalogIR],
    verb: str,
    job: str,
    target: str | None,
    *,
    include_all: bool,
    deep: bool,
    depth: int | None,
    as_json: bool,
    explain: bool,
) -> None:
    try:
        verb = validate_query(
            verb, target, depth=depth, deep=deep, include_all=include_all, explain=explain
        )
        catalog = load()
        if verb == "show":
            if job not in catalog.jobs:
                raise NavNotFound(f"job {job!r} not found")
            definition = job_to_source(catalog.jobs[job])
            typer.echo(
                json.dumps({"name": job, "definition": definition}) if as_json else definition
            )
            return
        matches = navigate(
            derive_graph(catalog),
            verb,
            job,
            target,
            depth=depth,
            deep=deep,
            include_all=include_all,
        )
    except NavNotFound as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except (ValueError, DslError, LoweringError, JilParseError, OSError) as exc:
        raise typer.Exit(refuse(exc)) from exc
    if as_json:
        typer.echo(json.dumps([match.name for match in matches]))
    else:
        for match in matches:
            typer.echo(f"{match.name}\t{match.atom}" if explain else match.name)


def nav(
    verb: str = typer.Argument(
        ..., help="fanins, fanouts, box, children, siblings, ancestors, descendants, path, show"
    ),
    estate: Path = typer.Argument(
        ..., help="JIL file, Python module/package, or estate TOML manifest."
    ),
    job: str = typer.Argument(..., help="Job name; path's starting job."),
    target: str | None = typer.Argument(None, help="path's destination job."),
    include_all: bool = typer.Option(
        False, "--all", help="Include mutex and unresolved job references."
    ),
    deep: bool = typer.Option(False, "--deep", help="Include nested children."),
    depth: int | None = typer.Option(
        None, "--depth", min=0, help="Maximum ancestor or descendant edge count."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print a JSON list, or an object for show."),
    explain: bool = typer.Option(
        False, "--explain", help="Append TAB and the edge atom in text output."
    ),
    environment: str | None = typer.Option(None, "--environment", help="Manifest environment."),
    properties: list[Path] = PROPERTIES,
    param: list[str] = typer.Option(
        [], "--param", help="Python Params binding NAME=VALUE; repeatable."
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
) -> None:
    """Inspect an estate without running it. Python estates execute their source on import.

    upstream/downstream alias fanins/fanouts. Exit 0 on success, 1 for a missing job
    or path, and 2 for usage or load errors. path follows producer to consumer.
    """
    _emit_navigation(
        lambda: load_navigation_catalog(estate, permit_unknown, properties, environment, param),
        verb,
        job,
        target,
        include_all=include_all,
        deep=deep,
        depth=depth,
        as_json=as_json,
        explain=explain,
    )


def estate_main(
    estate: Callable[[], CatalogBuilder], argv: Sequence[str], *, permit_unknown: bool
) -> None:
    """Dispatch nav over the same in-memory estate used by the decompiled footer."""
    app = typer.Typer(
        add_completion=False, context_settings={"help_option_names": ["-h", "--help"]}
    )

    def load() -> CatalogIR:
        try:
            builder = estate()
        except (Exception, SystemExit) as exc:
            raise ValueError(f"estate() raised {type(exc).__name__}: {exc}") from exc
        if not isinstance(builder, CatalogBuilder):
            raise ValueError(f"estate() returned {type(builder).__name__}, not a CatalogBuilder")
        return builder.build(permit_unknown=permit_unknown)

    @app.callback()
    def root() -> None:
        """Inspect this Python estate."""

    @app.command("nav")
    def embedded_nav(
        verb: str,
        job: str,
        target: str | None = typer.Argument(None),
        include_all: bool = typer.Option(False, "--all"),
        deep: bool = typer.Option(False, "--deep"),
        depth: int | None = typer.Option(None, "--depth", min=0),
        as_json: bool = typer.Option(False, "--json"),
        explain: bool = typer.Option(False, "--explain"),
    ) -> None:
        _emit_navigation(
            load,
            verb,
            job,
            target,
            include_all=include_all,
            deep=deep,
            depth=depth,
            as_json=as_json,
            explain=explain,
        )

    app(args=list(argv))
