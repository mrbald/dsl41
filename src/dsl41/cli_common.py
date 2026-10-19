"""Options, the catalog door and exit-code readings shared by more than one verb group (DL-137).

`cli.py` imports the verb modules, the verb modules import this one, and this one imports no CLI
module. Names here are public because a private name crossing a module boundary is a coupling
neither module promised (DL-74, DL-78; scripts/arch_check.py gate 2).
"""

from __future__ import annotations

import functools
import hashlib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import typer

from dsl41.ast_jil import JilFile, JilParseError, parse
from dsl41.ir import CatalogIR, LoweringError, lower_catalog
from dsl41.timezones import parse_timezone_map, resolve_timezone
from dsl41.placeholders import PlaceholderError, load_properties, substitute
from dsl41.semantics import help_text, parse_assignments
from dsl41.state_machine import EXIT_STATECHART_ERROR, StatechartError

if TYPE_CHECKING:
    from dsl41.boundary import EstateWalk, Lineage
    from dsl41.runner_clock import EngineError


# ------------------------------------------------- the shared options


CATALOG_FILES = typer.Argument(
    ...,
    help="JIL files and autocal calendar exports. Together they form one catalog.",
)

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
    " city name falls back to the one matching zoneinfo city, with a"
    " warning.",
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


# ------------------------------------------ addressing the whole estate


def walk_estate_or_exit_2(anchor_dir: Path) -> "EstateWalk":
    """ss1.3's archive registry, resolved for a verb, or exit 2.

    `audit`, `journal`, `runs` and `estate prune` enter their estate-wide mode through this door
    (PR-02f): the lineage ANCHOR stands where a run root would. A provisional registry row
    (`segment_durable: false`, ss1.3) is not a refusal; it is reported once here.
    """
    from dsl41.boundary import walk_estate
    from dsl41.runner_clock import EngineError

    try:
        walk = walk_estate(anchor_dir)
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc
    if walk.provisional:
        listed = ", ".join(str(number) for number in walk.provisional)
        typer.echo(
            f"note: period(s) {listed} have a registry row whose first segment is not"
            " durable yet, and every cross-period reader ignores a row until it is"
            " -- they are not in this total (period-model ss1.3)",
            err=True,
        )
    return walk


# ----------------------------------------- an answer becomes an exit code


def refuse(exc: BaseException | str, *, prefix: str = "") -> int:
    """Print one refusal on stderr and return the exit code it earns (DL-137).

    An input that never reached the tool, or a gate that refused a run, is exit 2. `prefix` goes
    before the colon. It returns the code so an async body can `return refuse(exc)` after its
    teardown, where a verb raises `typer.Exit(refuse(exc))`.
    """
    typer.echo(f"{prefix}: {exc}" if prefix else str(exc), err=True)
    return 2


def read_header_of(response: dict[str, Any]) -> "tuple[str, int] | None":
    """The concurrency-model ss6 read header off one control answer, or None after printing
    the refusal (DL-137)."""
    baseline, epoch = response.get("baseline_id"), response.get("epoch")
    if not isinstance(baseline, str) or not isinstance(epoch, int):
        typer.echo(str(response.get("error", "the engine answered no read header")), err=True)
        return None
    return baseline, epoch


def _pins(request: dict[str, Any]) -> str:
    """The original revision, epoch, baseline and actor pins (DL-217, DL-391).

    Empty for an envelope with no `expect`, such as the seal, whose retry restores its own baseline
    and epoch from the committed record (cli_estate).
    """
    import shlex

    expect = request.get("expect")
    if not isinstance(expect, dict) or len(expect) != 1:
        return ""
    (revision,) = expect.values()
    return (
        f" --expect {revision} --epoch {request.get('epoch')}"
        f" --baseline {shlex.quote(str(request.get('baseline_id')))}"
        + (
            f" --claimed-actor {shlex.quote(str(request['claimed_actor']))}"
            if "claimed_actor" in request
            else ""
        )
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

    The pins ride with it (DL-217): the fingerprint covers revision, epoch and baseline, so an id
    carried back alone is a collision once any of them has moved.
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
    rejected_as_unknown: bool = False,
    seal_in_flight_by_id: bool = False,
    retry_command: str | None = None,
) -> int:
    """Send one concurrency-model ss6 command envelope; return DL-92's outcome (0 applied, 2
    refused, 3 rejected, 4 unknown).

    One implementation serves `sendevent`/`host` and the live seal (DL-137). A dropped connection
    after the request left is `unknown` (4), not 2: the engine fsyncs an attempt before it feeds
    it, so 2's "safe to send again unchanged" would be false.

    `on_applied` adds the sentence a boundary prints when it committed.

    `rejected_as_unknown` is for a surface whose published table has no 3. `dsl41 seal` is one:
    period-model ss7 states 0/2/4/7 for it, and a live engine exits 3 when its period sealed
    (deployment-runbook ss6a). The ladder test in `tests/test_runner_control.py` drives a
    synthetic `rejected` reply through it and exits 4 (DL-145).

    `seal_in_flight_by_id`: a `seal_in_flight` refusal names the boundary in flight
    (`in_flight_request_id`, control-protocol ss3). If that is this request's own id the outcome
    is unknown (4). If it is another's, this request did nothing (2), with no promise that the
    period is still open.

    `retry_command` names the command an exact retry is sent as when it is not the one typed:
    `release-held` sends one OFF_HOLD per job, and its retry is the one-job `sendevent` (DL-217).
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
    if rejected_as_unknown and outcome == REJECTED:
        outcome = UNKNOWN
    if seal_in_flight_by_id and response.get("code") == "seal_in_flight":
        own = request.get("request_id")
        in_flight = response.get("in_flight_request_id")
        if in_flight == own:
            typer.echo(
                f"your seal {own} is still in flight; outcome unknown; retry later under"
                f" --request-id {own}",
                err=True,
            )
            return 4
        typer.echo(
            f"another boundary ({in_flight}) is in flight; this request did nothing", err=True
        )
        return 2
    if outcome == APPLIED and on_applied is not None:
        on_applied()
    if outcome == REFUSED:
        _collision(response)
    if outcome == UNKNOWN:
        _no_decision(request, retry_command)
    return {REFUSED: 2, REJECTED: 3, UNKNOWN: 4}.get(outcome, 0)


def active_period(run_root: Path) -> int:
    """The period this root's ACTIVE segment holds, or 1 where the root cannot say."""
    from dsl41.runner_history import RunHistoryError, active_period_id

    try:
        return active_period_id(run_root)
    except RunHistoryError:
        from dsl41.period import GENESIS_PERIOD_ID

        return GENESIS_PERIOD_ID


def lineage_of(run_root: Path) -> "Lineage | None":
    """ss11 step 3's answer for this root's active segment (`boundary.select_seal` over
    `estate_wal(run_root)`), or `None` when the root cannot be read.

    `None` on any read failure, not `EngineError` alone: `select_seal` reads its period numbers
    with a bare `int()`, so a hand-built record can raise `ValueError` or `KeyError`, and a damaged
    lineage must not become an exit-1 traceback (DL-149). Both `resume_target_period` and
    `cli_estate._committed_boundary` read it here (DL-169).
    """
    from dsl41.boundary import select_seal
    from dsl41.period import estate_wal
    from dsl41.runner_clock import EngineError
    from dsl41.runner_journal import read_journal

    try:
        return select_seal(run_root, read_journal(estate_wal(run_root)))
    except (OSError, ValueError, KeyError, EngineError):
        return None


def resume_target_period(run_root: Path) -> int:
    """The period a resume of this root OPENS INTO (period-model ss11 step 5).

    The newest segment's period, unless the root holds a committed boundary no engine has opened
    yet: then the resume opens N+1 under N+1's pin. Every CLI act on such a root is about that
    period (DL-151). An unreadable root falls back to the active period; the resume makes its own
    refusal (`lineage_of`'s `None`, DL-169). `Lineage.target_period` owns the opens_next-vs-seal
    rule (DL-171).
    """
    lineage = lineage_of(run_root)
    if lineage is not None:
        return lineage.target_period(active_period(run_root))
    return active_period(run_root)


def confirmed_resume_refusal(run_root: Path, anchor_dir: "Path | None") -> "EngineError | None":
    """ss1.3's resume rule (DL-224) for a CLI route that resumes: the refusal, or None.

    Call it before staging anything or wiring a supervisor, holding `leader.lock`. A refusal read
    without the anchor lock can be stale, so it stands only once a second read under that lock
    confirms it (`boundary.resume_root_refusal`, `locked=True`). Under the locks every refusal
    counts, a busy anchor lock included. `resume_run` repeats the check under both locks.
    """
    from dsl41.boundary import resume_root_refusal

    if resume_root_refusal(run_root, anchor_dir) is None:
        return None
    return resume_root_refusal(run_root, anchor_dir, locked=True)


def say_next(run_root: Path, estate_anchor: "Path | None") -> None:
    anchor = f" --estate-anchor {estate_anchor}" if estate_anchor is not None else ""
    typer.echo(
        f"open it with `dsl41 run --resume --run-root {run_root}{anchor} <new estate files>`,"
        f" or roll: `dsl41 audit --run-root {run_root}{anchor}` then"
        " `dsl41 run --open-from <anchor-dir> --run-root <new-root> <new estate files>`"
    )


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
    exits 7, naming the WAL input for a replay. cli.py wraps every verb that runs, replays or reads
    back an estate."""

    @functools.wraps(verb)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return verb(*args, **kwargs)
        except StatechartError as exc:
            statechart_exit(exc)

    return guarded
