"""What more than one CLI verb group needs (DL-137's split).

Three things live here and nothing else does: the options several verb
groups declare, the door every catalog-consuming verb loads through, and
the readings that turn an answer -- an exception, a control-socket
response -- into this surface's exit code.

The layering rule is one-directional and is the whole point: `cli.py`
imports the verb modules, the verb modules import this one, and this one
imports no CLI module at all. Names here are public because a private name
crossing a module boundary is a coupling neither module promised (DL-74,
DL-78; scripts/arch_check.py gate 2).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from pathlib import Path

import typer

from dsl41.ast_jil import JilFile, JilParseError, parse
from dsl41.ir import CatalogIR, LoweringError, lower_catalog
from dsl41.placeholders import PlaceholderError, load_properties, substitute


# ------------------------------------------------- the shared options


PERMIT_UNKNOWN = typer.Option(
    False,
    "--permit-unknown",
    help="Carry unknown attributes verbatim instead of refusing (DL-07 escape hatch).",
)

PROPERTIES = typer.Option(
    None,
    "--properties",
    "-p",
    help="Resolve ~{$NAME}~ placeholders from these properties file(s) before parsing"
    " (repeatable; later files override earlier, DL-19/DL-22).",
)


TIMEZONE_OPT = typer.Option(
    None,
    "--timezone",
    help="Base zone for schedules without a per-job timezone (PENDING: E10;"
    " default UTC -- vendor uses the server's zone).",
)

TIMEZONE_MAP_OPT = typer.Option(
    None,
    "--timezone-map",
    help="File resolving vendor timezone names (SEM-35/DL-62): the instance's"
    " `autotimezone -l` listing verbatim, or bare 'name zone' pairs. Without"
    " it, an unknown city name falls back to the unique zoneinfo city match"
    " with a preflight WARN.",
)


# --------------------------------------------------- the catalog door


def load_catalog_or_exit_2(
    files: Iterable[Path],
    permit_unknown: bool,
    properties: list[Path] | None = None,
) -> CatalogIR:
    return load_catalog_and_ast_or_exit_2(files, permit_unknown, properties)[0]


def load_catalog_and_ast_or_exit_2(
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
        raise typer.Exit(refuse(exc)) from exc


def load_tz_aliases(path: "Path | None") -> "dict[str, str] | None":
    """Parse --timezone-map (DL-62); unreadable/malformed exits 2."""
    if path is None:
        return None
    from dsl41.runner_scheduler import parse_timezone_map

    try:
        return parse_timezone_map(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise typer.Exit(refuse(exc, prefix=f"--timezone-map {path}")) from exc


def check_base_tz(timezone: str | None, tz_aliases: "dict[str, str] | None" = None) -> None:
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


def import_tui_or_exit_2():
    """Guarded textual import (runner-design ss14): the core package keeps
    its three runtime deps; the TUI is the optional [ui] extra."""
    try:
        from dsl41 import runner_tui
    except ModuleNotFoundError as exc:
        raise typer.Exit(
            refuse("the TUI needs the optional [ui] extra: pip install 'dsl41[ui]'")
        ) from exc
    return runner_tui


# ----------------------------------------- an answer becomes an exit code


def refuse(exc: BaseException | str, *, prefix: str = "") -> int:
    """Print one refusal on stderr and answer with the exit code it earns.

    ONE spelling of the rule the CLI's exit-code contract states (DL-137;
    the copies this replaced said it once per verb): an input that never
    reached the tool, or a gate that would not let a run start, is exit 2.
    `prefix` is what the caller puts before the colon -- the option, the
    socket, the root -- and an empty one prints the refusal alone.

    It RETURNS the code rather than raising it because the surfaces
    legitimately differ and only there: a verb raises
    `typer.Exit(refuse(exc))`, an async body that still owes a lock or a
    supervisor its teardown does `return refuse(exc)`.
    """
    typer.echo(f"{prefix}: {exc}" if prefix else str(exc), err=True)
    return 2


def read_header_of(response: dict) -> "tuple[str, int] | None":
    """The ss6 read header off one control answer, or None after printing
    the refusal -- the ONE check (DL-137): two verbatim copies differed
    only in how they exited."""
    baseline, epoch = response.get("baseline_id"), response.get("epoch")
    if not isinstance(baseline, str) or not isinstance(epoch, int):
        typer.echo(str(response.get("error", "the engine answered no read header")), err=True)
        return None
    return baseline, epoch


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


def command_outcome(
    socket_path: Path,
    request: dict,
    *,
    on_applied: Callable[[], None] | None = None,
    rejected_as_unknown: bool = False,
) -> int:
    """Send one ss6 command envelope and answer with its outcome: DL-92's
    four (0 applied / 2 refused / 3 rejected / 4 unknown).

    ONE implementation for `sendevent`/`host` and for the live seal,
    because reading an answer is the PROTOCOL's business, not the verb's
    -- and because the transport half has to read the same way at every
    one of them, which is the half that was wrong. A dropped connection
    used to land on exit 2 wherever it happened, and 2 promises the log
    says nothing about the command and it is safe to send again unchanged.
    That promise holds for a request that never left; for one that left and
    got no answer it is exactly backwards, because the engine fsyncs an
    attempt before it feeds it. Delivered-and-unanswered is the case
    `unknown` exists for.

    `on_applied` is the one thing a caller may add to the ladder: the
    boundary has a sentence to print when it committed and a mutation does
    not (DL-137 -- the seal's copy of this ladder classified its answer by
    hand, which is how the classifier's `rejected` reading went missing
    from one of the two).

    `rejected_as_unknown` is for a surface whose PUBLISHED table has no 3.
    `dsl41 seal` is the one: period-model ss7 states 0/2/4 for it, and 3 is
    taken in that same feature area -- a live ENGINE exits 3 when its
    period sealed, and init units read that number
    (deployment-runbook ss6a). Nothing reaches the fold today, because the
    engine's `seal` handler answers `ok`, `refused` or a bare timeout and
    never a decision; it keeps that verb's answer to an outcome it cannot
    classify exactly what it was before this ladder was shared. Widening
    ss7's table is ss7's call, not this slice's.
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

    try:
        response = roundtrip(socket_path, request)
    except ControlClientError as exc:
        code = refuse(exc)
        if not exc.delivered:
            return code
        _no_decision(request)
        return 4
    typer.echo(json_mod.dumps(response, sort_keys=True))
    outcome = outcome_of(response)
    if rejected_as_unknown and outcome == REJECTED:
        outcome = UNKNOWN
    if outcome == APPLIED and on_applied is not None:
        on_applied()
    if outcome == UNKNOWN:
        _no_decision(request)
    return {REFUSED: 2, REJECTED: 3, UNKNOWN: 4}.get(outcome, 0)


def say_next(run_root: Path, estate_anchor: "Path | None") -> None:
    anchor = f" --estate-anchor {estate_anchor}" if estate_anchor is not None else ""
    typer.echo(
        f"open it with `dsl41 run --resume --run-root {run_root}{anchor} <new estate files>`,"
        f" or roll: `dsl41 audit --run-root {run_root}{anchor}` then"
        " `dsl41 run --open-from <anchor-dir> --run-root <new-root> <new estate files>`"
    )
