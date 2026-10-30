"""Options, the catalog door and exit-code readings shared by more than one verb group (DL-137).

`cli.py` imports the verb modules, the verb modules import this one, and this one imports no CLI
module. Names here are public because a private name crossing a module boundary is a coupling
neither module promised (DL-74, DL-78; scripts/arch_check.py gate 2).
"""

from __future__ import annotations

import functools
import hashlib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn

import typer

from dsl41.ast_jil import JilFile, JilParseError, parse
from dsl41.estate_manifest import ManifestError, check_absolute, resolve_manifest
from dsl41.ir import CatalogIR, LoweringError, lower_catalog
from dsl41.timezones import parse_timezone_map, resolve_timezone
from dsl41.placeholders import PlaceholderError, load_properties, substitute
from dsl41.semantics import help_text, parse_assignments
from dsl41.state_machine import EXIT_STATECHART_ERROR, StatechartError

if TYPE_CHECKING:
    from dsl41.dsl import CatalogBuilder


# ------------------------------------------------- the shared options


CATALOG_FILES = typer.Argument(
    ...,
    help="JIL files and autocal calendar exports. Together they form one catalog.",
)  # a verb that also reads a manifest takes ESTATE_FILES below (DL-422)

PERMIT_UNKNOWN = typer.Option(
    False,
    "--permit-unknown",
    help="Accept unknown attributes and carry them through unchanged, instead of"
    " refusing the input.",
)  # DL-07

PROPERTIES = typer.Option(
    None,
    "--properties",
    "-p",
    help="Properties files with KEY=VALUE lines. Their values fill ~{$NAME}~"
    " placeholders before parsing. Repeatable; later files override earlier"
    " ones.",
)  # DL-19/DL-22


TIMEZONE_OPT = typer.Option(
    None,
    "--timezone",
    help="Timezone for schedules that do not set their own. Default UTC. AutoSys"
    " uses the scheduler's zone, so set the server's zone here when migrating"
    " an estate.",
)  # DL-155

TIMEZONE_MAP_OPT = typer.Option(
    None,
    "--timezone-map",
    help="File that maps AutoSys timezone names to zones: the output of"
    " 'autotimezone -l', or plain 'name zone' lines. Without it, an unknown"
    " city name is read as the one matching zoneinfo city, with a warning.",
)  # SEM-35/DL-62

SEMANTICS_OPT = typer.Option(
    [],
    "--semantics",
    metavar="NAME=VALUE",
    help="Choose how one behavior is read where AutoSys and dsl41's default"
    " can differ. Repeatable. The choice is recorded with the run, and a"
    " resume must repeat it. The section on semantic switches in"
    " docs/runner-design.md explains each one. Switches: " + help_text(),
)  # runner-design ss8a, DL-252


#: The catalog of a verb that also reads an estate manifest: optional, because `--manifest`
#: supplies it instead (DL-422). Every other verb keeps CATALOG_FILES, which is required.
ESTATE_FILES = typer.Argument(
    None,
    metavar="[FILES]...",
    show_default=False,
    help="JIL files and autocal calendar exports. Together they form one catalog."
    " Give them or --manifest.",
)

MANIFEST_OPT = typer.Option(
    None,
    "--manifest",
    metavar="PATH",
    help="The estate manifest, estate.toml, as an absolute path. It supplies the files, the"
    " properties layers, the timezone, its map and the semantic switches, so give none of them"
    " beside it; -p adds layers after the manifest's.",
)  # DL-422

ENVIRONMENT_OPT = typer.Option(
    None,
    "--environment",
    metavar="NAME",
    help="The manifest's environment, whose properties layers follow the manifest's own."
    " Needs --manifest; required when the manifest declares environments.",
)  # DL-422


class EstateArgs(NamedTuple):
    """A verb's estate inputs as its own parameters hold them: the files, the `-p` layers in
    order, the base timezone, the timezone map and the semantic switches as NAME=VALUE."""

    files: list[Path]
    properties: list[Path]
    timezone: str | None
    timezone_map: Path | None
    semantics: list[str]


def check_estate_args(
    manifest: Path | None,
    environment: str | None,
    given: EstateArgs,
    *,
    files_word: str = "FILES",
) -> None:
    """The refusals of `--manifest` that read no file, each one sentence and exit 2 (DL-422).

    Beside `--manifest`, the files and every interpretation option are refused outright, whether
    or not the manifest sets the value: an omitted field is an authoritative default, and a
    command-line value would give the opener a profile the seal did not state."""
    if manifest is None:
        if environment is not None:
            raise typer.Exit(refuse("--environment needs --manifest."))
        if not given.files:
            raise typer.Exit(refuse(f"give {files_word} or --manifest."))
        return
    interpretation = (given.timezone, given.timezone_map)
    if given.files or given.semantics or any(value is not None for value in interpretation):
        raise typer.Exit(
            refuse("--manifest supplies the files and the interpretation options; give either.")
        )
    try:
        check_absolute(manifest)
    except ManifestError as exc:
        raise typer.Exit(refuse(exc)) from exc


def estate_args(
    manifest: Path | None,
    environment: str | None,
    files: "list[Path] | None",
    properties: "list[Path] | None",
    timezone: str | None = None,
    timezone_map: Path | None = None,
    semantics: "list[str] | None" = None,
    *,
    files_word: str = "FILES",
) -> EstateArgs:
    """The estate inputs a verb loads: as the command line gave them, or, with MANIFEST, the
    ones it declares under ENVIRONMENT, its layers first and the command line's `-p` after them
    (DL-422). The expansion sets the verb's own parameters, so its one loader, parser and hash
    see a manifest's estate as they see the same inputs typed out. Each path is the manifest's
    directory joined to the name, unresolved (DL-407, period-model ss1.1). A refusal is one
    sentence and exit 2, before the verb writes anything."""
    given = EstateArgs(
        list(files or []), list(properties or []), timezone, timezone_map, list(semantics or [])
    )
    check_estate_args(manifest, environment, given, files_word=files_word)
    if manifest is None:
        return given
    try:
        inputs = resolve_manifest(manifest, environment)
    except ManifestError as exc:
        raise typer.Exit(refuse(exc)) from exc
    return EstateArgs(
        files=[Path(file) for file in inputs.files],
        properties=[*(Path(layer) for layer in inputs.layers), *given.properties],
        timezone=inputs.timezone,
        timezone_map=None if inputs.timezone_map is None else Path(inputs.timezone_map),
        semantics=[f"{name}={value}" for name, value in inputs.semantics.items()],
    )


def load_semantics(items: "list[str] | None", *, option: str = "--semantics") -> dict[str, str]:
    """Parse repeated NAME=VALUE switches; an unknown name or value exits 2 (DL-252)."""
    try:
        return parse_assignments(items or ())
    except ValueError as exc:
        raise typer.Exit(refuse(exc, prefix=option)) from exc


# --------------------------------------------------- the catalog door


def load_catalog_or_exit_2(
    files: Iterable[Path],
    permit_unknown: bool,
    properties: list[Path] | None = None,
) -> CatalogIR:
    return load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)[0]


#: The exit-2 class: every way an input can fail to reach the tool (DL-75). `LoweringError` is
#: in the tuple because the catalog door lowers; the read and parse half cannot raise it.
_EXIT_2_ERRORS = (JilParseError, LoweringError, PlaceholderError, OSError, UnicodeDecodeError)


def module_builder(module: Path, params: "Mapping[str, str] | None" = None) -> "CatalogBuilder":
    """Import MODULE from its path and return what its estate() returns, or exit 2 (DL-410).

    MODULE is a file or a package directory with an `__init__.py`, whose modules import each
    other relatively (DL-418). Until estate() returns, the directory that holds MODULE leads
    sys.path, as it does for `python MODULE`, so an estate may import its sibling modules, at
    import or inside estate(). Nothing writes bytecode beside the source. The module is
    registered in sys.modules under a name no real module has, so code that looks itself up
    there (a dataclass under postponed annotations) works. Afterwards the module and its
    submodules leave sys.modules, and so does every module newly imported through the
    directory's sys.path entry: a sibling file, or a sibling package and its submodules. A later
    load in the same process then imports its own siblings. A module found through another
    sys.path entry stays, even one below the directory, such as a project's `.venv` or `src`.

    With PARAMS, estate() is called with the module's `Params(**PARAMS)` (DL-418)."""
    import importlib.util
    import os
    import sys

    from dsl41.dsl import CatalogBuilder

    name = f"dsl41_build_{module.name.replace('.', '_')}_{os.getpid()}"
    if module.is_dir():
        if not (module / "__init__.py").is_file():
            raise typer.Exit(refuse(f"{module}: a directory with no __init__.py"))
        spec = importlib.util.spec_from_file_location(
            name, module / "__init__.py", submodule_search_locations=[str(module)]
        )
    else:
        spec = importlib.util.spec_from_file_location(name, module)
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
        arguments = [] if params is None else [_params_value(module, loaded, params)]
        try:
            builder = function(*arguments)
        except (Exception, SystemExit) as exc:
            raise typer.Exit(
                refuse(f"{module}: estate() raised {type(exc).__name__}: {exc}")
            ) from exc
    finally:
        sys.dont_write_bytecode = write_bytecode
        sys.path.remove(str(directory))
        for imported in set(sys.modules) - known:
            origin = getattr(sys.modules[imported], "__file__", None)
            top = directory / imported.partition(".")[0]
            if imported.partition(".")[0] == spec.name or (
                origin is not None
                and (
                    Path(origin).resolve() == top.with_name(f"{top.name}.py")
                    or Path(origin).resolve().is_relative_to(top)
                )
            ):
                del sys.modules[imported]
    if not isinstance(builder, CatalogBuilder):
        raise typer.Exit(
            refuse(f"{module}: estate() returned {type(builder).__name__}, not a CatalogBuilder")
        )
    return builder


def _params_value(module: Path, loaded: object, params: "Mapping[str, str]") -> object:
    """MODULE's `Params(**PARAMS)`, or exit 2 (DL-418)."""
    factory = getattr(loaded, "Params", None)
    if not isinstance(factory, type):
        raise typer.Exit(refuse(f"{module}: --param needs a Params class, and there is none"))
    try:
        return factory(**params)
    except TypeError as exc:
        raise typer.Exit(refuse(f"{module}: --param does not fit Params: {exc}")) from exc


class ModuleJil(NamedTuple):
    """What a module's estate() declares, lowered once as one estate (DL-410, DL-418)."""

    whole: bytes  # the estate's JIL
    files: dict[str, bytes]  # each file's JIL when estate() composes files, else empty


def module_jil(
    module: Path, permit_unknown: bool, params: "Mapping[str, str] | None" = None
) -> ModuleJil:
    """The JIL that MODULE's estate() declares, lowered through the compiler, or exit 2.

    `dsl41 build` and `estate build` both render a module through this one loader, which
    imports MODULE once per call (DL-410, DL-413). A composed estate lowers whole, and each of
    its files renders apart (DL-418)."""
    from dsl41.dsl import DslError

    builder = module_builder(module, params)
    try:
        builder.build(permit_unknown=permit_unknown)
        files = builder.file_jils()
    except (JilParseError, LoweringError) as exc:
        raise typer.Exit(refuse(exc, prefix=f"{module}: the built JIL does not lower")) from exc
    except DslError as exc:
        raise typer.Exit(refuse(exc, prefix=f"{module}")) from exc
    return ModuleJil(
        builder.to_jil().encode("utf-8"),
        {name: text.encode("utf-8") for name, text in files.items()},
    )


def parse_files_or_exit_2(
    files: Iterable[Path],
    properties: list[Path] | None = None,
) -> tuple[list[JilFile], dict[str, str]]:
    """Read, substitute and parse; returns (parsed ASTs, input fingerprint).

    The fingerprint (path -> sha256) is the ss10 spec_drift baseline (DL-65). It hashes the bytes
    this load parsed, and an unreadable input stays an exit-2 refusal.
    """
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
        return parsed, fingerprint
    except _EXIT_2_ERRORS as exc:
        # unreadable input (missing, a directory, non-UTF-8) never reached the tool: exit 2
        raise typer.Exit(refuse(exc)) from exc


def load_catalog_and_ast_or_exit_2(
    files: Iterable[Path],
    permit_unknown: bool,
    properties: list[Path] | None = None,
) -> tuple[CatalogIR, list[JilFile], dict[str, str]]:
    """Returns (catalog, parsed ASTs, input fingerprint); lowering failures exit 2."""
    parsed, fingerprint = parse_files_or_exit_2(files, properties)
    try:
        return lower_catalog(parsed, permit_unknown=permit_unknown), parsed, fingerprint
    except _EXIT_2_ERRORS as exc:
        raise typer.Exit(refuse(exc)) from exc


def load_tz_aliases(path: "Path | None") -> "dict[str, str] | None":
    """Parse --timezone-map (DL-62); unreadable/malformed exits 2."""
    if path is None:
        return None
    try:
        return parse_timezone_map(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise typer.Exit(refuse(exc, prefix=f"--timezone-map {path}")) from exc


def check_base_tz(timezone: str | None, tz_aliases: "dict[str, str] | None" = None) -> None:
    """Preflight the run-level base zone (per-job zones gate in ss8): --timezone exits 2 (DL-45)."""
    if timezone is None:
        return
    if resolve_timezone(timezone, tz_aliases) is None:
        raise typer.Exit(
            refuse(
                f"--timezone {timezone!r} is not resolvable (SEM-35: zoneinfo, the"
                " --timezone-map table, or a POSIX fixed offset)"
            )
        )


def import_tui_or_exit_2():
    """Guarded textual import (runner-design ss14): the TUI is the optional [ui] extra."""
    try:
        from dsl41 import runner_tui
    except ModuleNotFoundError as exc:
        raise typer.Exit(
            refuse("the TUI needs the optional [ui] extra: pip install 'dsl41[ui]'")
        ) from exc
    return runner_tui


# ----------------------------------------- an answer becomes an exit code


def refuse(exc: BaseException | str, *, prefix: str = "") -> int:
    """Print one refusal on stderr and return the exit code it earns (DL-137).

    An input that never reached the tool, or a gate that refused a run, is exit 2. `prefix` goes
    before the colon. It returns the code so an async body can `return refuse(exc)` after its
    teardown, where a verb raises `typer.Exit(refuse(exc))`.
    """
    typer.echo(f"{prefix}: {exc}" if prefix else str(exc), err=True)
    return 2


def read_header_of(response: dict[str, Any]) -> str | None:
    """The concurrency-model ss6 read header's `baseline_id` off one control answer, or None
    after printing the refusal (DL-137)."""
    baseline = response.get("baseline_id")
    if not isinstance(baseline, str):
        typer.echo(str(response.get("error", "the engine answered no read header")), err=True)
        return None
    return baseline


def _pins(request: dict[str, Any]) -> str:
    """The original revision, baseline and actor pins (DL-217, DL-391). Empty for an envelope
    with no `expect`."""
    import shlex

    expect = request.get("expect")
    if not isinstance(expect, dict) or len(expect) != 1:
        return ""
    (revision,) = expect.values()
    return f" --expect {revision} --baseline {shlex.quote(str(request.get('baseline_id')))}" + (
        f" --claimed-actor {shlex.quote(str(request['claimed_actor']))}"
        if "claimed_actor" in request
        else ""
    )


def _retry_as(request: dict[str, Any], retry_command: str | None) -> str:
    """The exact retry: the id plus the pins, prefixed by the command when it differs."""
    import shlex

    flags = f"--request-id {shlex.quote(str(request['request_id']))}{_pins(request)}"
    return f"{retry_command} {flags}" if retry_command else flags


def _sending(request: dict[str, Any], retry_command: str | None) -> None:
    """The pre-send line (DL-217): the exact-retry flags, quoted for a shell.

    It is printed and flushed before the first write, so it survives a client that dies
    mid-exchange. It never carries the payload. It is a record, printed for every mutation; the
    exit-4 line is the only one that advises "retry ONLY as".
    """
    typer.echo(f"sending: {_retry_as(request, retry_command)}", err=True)  # echo flushes


def _no_decision(request: dict[str, Any], retry_command: str | None = None) -> None:
    """DL-92's fourth outcome. The id goes to stderr because it is what makes the retry safe.

    The pins ride with it (DL-217): the fingerprint covers revision and baseline, so an id
    carried back alone is a collision once either has moved.
    """
    typer.echo(
        f"no decision: this command may still apply. Re-read, then retry ONLY as"
        f" {_retry_as(request, retry_command)}",
        err=True,
    )


def _collision(response: dict[str, Any]) -> None:
    """Both facts of a collision refusal (DL-217): this request was refused, and its id already
    holds an earlier decision. The exit stays the refusal's 2."""
    from dsl41.runner_control import original_decision_text

    original = original_decision_text(response)
    if original is None:
        return
    typer.echo(f"refused: {response.get('error', 'request_id collision')}", err=True)
    typer.echo(original, err=True)


def command_outcome(
    socket_path: Path,
    request: dict[str, Any],
    *,
    on_applied: Callable[[], None] | None = None,
    retry_command: str | None = None,
) -> int:
    """Send one concurrency-model ss6 command envelope; return DL-92's outcome (0 applied, 2
    refused, 3 rejected, 4 unknown).

    One implementation serves `sendevent` and `off-hold-all` (DL-137). A dropped connection
    after the request left is `unknown` (4), not 2: the engine fsyncs an attempt before it feeds
    it, so 2's "safe to send again unchanged" would be false.

    `on_applied` adds a sentence when the command applied.

    `retry_command` names the command an exact retry is sent as when it is not the one typed:
    `off-hold-all` sends one OFF_HOLD per job, and its retry is the one-job `sendevent` (DL-217).
    """
    import json as json_mod

    from dsl41.runner_control import (
        APPLIED,
        REFUSED,
        REJECTED,
        UNKNOWN,
        ControlClientError,
        outcome_of,
        roundtrip,
    )

    _sending(request, retry_command)
    try:
        response = roundtrip(socket_path, request)
    except ControlClientError as exc:
        code = refuse(exc)
        if not exc.delivered:
            return code
        _no_decision(request, retry_command)
        return 4
    typer.echo(json_mod.dumps(response, sort_keys=True))
    outcome = outcome_of(response)
    if outcome == APPLIED and on_applied is not None:
        on_applied()
    if outcome == REFUSED:
        _collision(response)
    if outcome == UNKNOWN:
        _no_decision(request, retry_command)
    return {REFUSED: 2, REJECTED: 3, UNKNOWN: 4}.get(outcome, 0)


def statechart_report(exc: StatechartError) -> int:
    """Print a `StatechartError`'s report on stderr under its prefix; return exit code 7
    (docs/statechart-engine.md ss5)."""
    typer.echo(exc.report(), err=True)
    return EXIT_STATECHART_ERROR


def statechart_exit(exc: StatechartError) -> NoReturn:
    """`statechart_report`, then exit 7."""
    raise typer.Exit(statechart_report(exc)) from exc


def stops_on_statechart_error[**P, R](verb: Callable[P, R]) -> Callable[P, R]:
    """Wrap a verb so a `StatechartError` from anything it runs or replays prints the report and
    exits 7, naming the recorded input for a replay. cli.py wraps every verb that runs, replays or reads
    back an estate."""

    @functools.wraps(verb)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return verb(*args, **kwargs)
        except StatechartError as exc:
            statechart_exit(exc)

    return guarded
