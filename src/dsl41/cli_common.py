"""Options, the catalog door and exit-code readings shared by more than one verb group (DL-137).

`cli.py` imports the verb modules, the verb modules import this one, and this one imports no CLI
module. Names here are public because a private name crossing a module boundary is a coupling
neither module promised (DL-74, DL-78; scripts/arch_check.py gate 2).
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn

import typer

from dsl41.ast_jil import JilFile, JilParseError, parse
from dsl41.ir import CatalogIR, LoweringError, lower_catalog
from dsl41.timezones import parse_timezone_map, resolve_timezone
from dsl41.placeholders import PlaceholderError, load_properties, substitute
from dsl41.semantics import help_text, parse_assignments
from dsl41.state_machine import EXIT_STATECHART_ERROR, StatechartError

if TYPE_CHECKING:
    from dsl41.boundary import EstateWalk, Lineage
    from dsl41.dsl import CatalogBuilder
    from dsl41.runner_adapters import SupervisorConn
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


class SupervisorChoice(str, Enum):
    """`--supervisor`: how a detached launch gets its supervisor (DL-396).

    `attach` connects to the supervisor already running on the run root and starts none. `spawn`
    starts one when none answers. `run` defaults to `spawn` and the offline `seal` to `attach`."""

    attach = "attach"
    spawn = "spawn"


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


#: how long past the supervisor's own bound a client watches an admitted operation (DL-401)
MAINTENANCE_MARGIN_S = 2.0
#: exit 2: a host signal started the supervisor's drain, which refuses maintenance (DL-416)
DRAINING = "supervisor is draining; wait for the unit to stop"
#: exit 3: the wait ended on an operation that still runs (DL-401)
IN_PROGRESS = "in progress; it finishes on its own; read maintenance.json or run this again"
#: the process states of an admitted operation, running or uncertain (DL-401)
_OPERATION_STATES = frozenset(
    {"maintenance_stopping", "maintenance_completing", "maintenance_uncertain"}
)
#: the process states of an operation still being run (DL-401)
_RUNNING_STATES = frozenset({"maintenance_stopping", "maintenance_completing"})
#: the refusals after which nothing changed: exit 2 (DL-390)
_MAINTENANCE_REFUSALS = frozenset(
    {
        "maintenance_refused",
        "wrong_incarnation",
        "stale_token",
        "unknown_verb",
        "unsupported_version",
    }
)


def supervisor_exited(pid: int, start: str) -> bool:
    """Whether the process named by (pid, start token) has exited. Only ESRCH proves absence
    when the identity cannot be read (DL-390); a zombie or a reused pid has exited."""
    from dsl41.runner_procid import proc_is_zombie, proc_start_token

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    observed = proc_start_token(pid)
    if observed is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        raise OSError("cannot observe the supervisor process identity")
    return observed != start or proc_is_zombie(pid)


def _left_of(record: dict[str, Any]) -> float:
    """The rest of a recorded operation's bound, from the wall clock both processes read, held
    within the bound, plus the client's margin (DL-401)."""
    from datetime import datetime

    started = datetime.fromisoformat(record["started_at"]).timestamp()
    bound = float(record["bound_s"])
    return min(max(started + bound - time.time(), 0.0), bound) + MAINTENANCE_MARGIN_S


def settle_maintenance(
    run_root: Path,
    incarnation: str | None,
    *,
    within_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    probe: Callable[[], dict[str, Any]] | None = None,
) -> tuple[int, str, dict[str, Any] | None]:
    """Watch an admitted operation to its outcome from `maintenance.json` (DL-401): 0 when the
    named supervisor has exited and the record says completed; 3 when it is still running as
    the wait ends; 4 when the outcome cannot be observed: the record says uncertain, is
    missing or unreadable, does not say completed after the exit, or names no supervisor
    this caller can tie it to. `incarnation` is the supervisor the caller knows; None takes
    the record's own, which must then name the root's last supervisor. `within_s` replaces
    the rest of the recorded bound. 3 needs `probe`, one PING, to answer with this operation
    still running; a live identity that answers nothing is 4, since nothing more is
    scheduled. Returns the code, its reason and the last record read. Sends nothing else."""
    from dsl41.runner_procid import current_boot_id
    from dsl41.runner_supervisor import read_maintenance_record, record_owner_problem

    record = read_maintenance_record(run_root)
    if record is None or incarnation not in (None, record["incarnation"]):
        return 4, "no maintenance.json names this supervisor", record
    if record["boot_id"] != current_boot_id():
        return 4, "maintenance.json names another boot", record
    if incarnation is None and (problem := record_owner_problem(run_root, record)) is not None:
        return 4, problem, record
    incarnation = record["incarnation"]
    pid, start = record["supervisor_pid"], record["supervisor_start_time"]
    deadline = time.monotonic() + (_left_of(record) if within_s is None else within_s)
    while True:
        exited = supervisor_exited(pid, start)
        # read after the exit check, so an exit observed is read with its final record
        record = read_maintenance_record(run_root)
        state = None if record is None else record["state"]
        if record is not None and record["incarnation"] != incarnation:
            state = None  # a later supervisor's record says nothing of this one
        if exited:
            if state == "completed":
                return 0, f"completed; the supervisor (pid {pid}) exited", record
            return 4, f"the supervisor (pid {pid}) exited and its record says {state}", record
        if state == "uncertain":
            assert record is not None
            return 4, f"maintenance is uncertain: {record['detail']}", record
        if time.monotonic() >= deadline:
            if state not in ("admitted", "completed"):
                return 4, f"the supervisor (pid {pid}) is alive and its record says {state}", record
            answer = {} if probe is None else probe()
            if (
                answer.get("ok") is True
                and answer.get("incarnation") == incarnation
                and answer.get("process_state") in _RUNNING_STATES
            ):
                return 3, IN_PROGRESS, record
            return (
                4,
                f"the supervisor (pid {pid}) is alive without a running operation on its socket"
                f" ({answer.get('process_state', 'no answer')}); read supervisor.log before"
                " anything else",
                record,
            )
        sleep(0.05)


def ping_probe(
    sock_path: Path, connect: Callable[[Path], SupervisorConn]
) -> Callable[[], dict[str, Any]]:
    """One PING on a fresh connection, or {} when nothing answers: the evidence exit 3 needs
    that an admitted operation is still being run (DL-401)."""
    from dsl41.runner_clock import EngineError

    def probe() -> dict[str, Any]:
        try:
            conn = connect(sock_path)
            try:
                return conn.send({"cmd": "PING"})
            finally:
                conn.close()
        except (OSError, ValueError, EngineError):
            return {}

    return probe


def settle_rerun(run_root: Path, sleep: Callable[[float], None] = time.sleep) -> int:
    """A rerun that finds no supervisor socket and a `maintenance.json`: the record's own
    operation, watched to its outcome as `settle_maintenance` does (DL-401)."""
    try:
        code, reason, record = settle_maintenance(run_root, None, sleep=sleep)
    except (OSError, ValueError) as exc:
        code, reason, record = 4, str(exc), None
    return _settled(code, reason, record, run_root / "supervisor.sock")[0]


def _lost_reply(
    sock_path: Path,
    connect: Callable[[Path], SupervisorConn],
    *,
    controller_id: str,
    incarnation: str,
    token: int,
    sleep: Callable[[float], None],
) -> tuple[int, str, dict[str, Any] | None]:
    """A MAINTENANCE_STOP whose reply was lost after the send (DL-401). It is never resent.
    One reconnect presents the request's own lease to ACQUIRE: either answer ends that token,
    so a request not yet read is refused stale_token and cannot run later. PING then says
    whether it ran. `serving` with no record of this incarnation is "not admitted"; a
    maintenance state, or a supervisor gone, is settled from maintenance.json."""
    from dsl41.runner_clock import EngineError
    from dsl41.runner_supervisor import read_maintenance_record

    ping: dict[str, Any] = {}
    try:
        conn = connect(sock_path)
        try:
            conn.incarnation = incarnation
            conn.send(
                {"cmd": "ACQUIRE", "controller_id": controller_id, "ttl_s": 60, "token": token}
            )
            ping = conn.send({"cmd": "PING"})
        finally:
            conn.close()
    except (OSError, ValueError, EngineError):
        pass  # no supervisor answers: maintenance.json decides
    if ping.get("ok") is True and ping.get("incarnation") == incarnation:
        if ping.get("process_state") == "draining":
            # an admitted operation holds every signal, so a draining one admitted nothing
            return 2, DRAINING, None
        if ping.get("process_state") == "serving":
            record = read_maintenance_record(sock_path.parent)
            if record is None or record["incarnation"] != incarnation:
                return 2, "not admitted; nothing changed", None
            return 4, "the supervisor serves and maintenance.json says it was admitted", record
        if ping.get("process_state") not in _OPERATION_STATES:
            return 4, f"the supervisor answers {ping.get('process_state')}", None
    return settle_maintenance(
        sock_path.parent, incarnation, sleep=sleep, probe=ping_probe(sock_path, connect)
    )


def _settled(
    code: int, reason: str, record: dict[str, Any] | None, sock_path: Path
) -> tuple[int, dict[str, Any] | None]:
    """Print a settled operation: the completed record on stdout, else the reason. A 2 (a lost
    request that was never admitted) answers as a refusal would, `not_admitted`."""
    if code == 0:
        typer.echo(json.dumps(record, sort_keys=True))
        return 0, record
    refuse(reason, prefix=f"supervisor {sock_path}")
    if code == 2:
        return 2, {"ok": False, "error": "not_admitted", "detail": reason}
    return code, None


def guarded_maintenance(
    conn: SupervisorConn,
    sock_path: Path,
    *,
    controller_id: str,
    force: str | None,
    before_stop: Callable[[], None] | None = None,
    on_refusal: Callable[[str, int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    connect: Callable[[Path], SupervisorConn] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """The client half of guarded maintenance (DL-390, DL-401): PING, ACQUIRE, MAINTENANCE_STOP,
    then the admitted operation watched to its outcome in `maintenance.json`, within its
    `bound_s` and a margin. Returns 0 completed, 2 refused or not admitted, 3 admitted and still
    running when the wait ends, or 4 unknown, with the final record, or the refusal reply
    (ACQUIRE's when the lease is refused). A PING that shows an operation already admitted
    watches it and sends nothing. A lost reply is never resent: one reconnect resolves it
    (`_lost_reply`). `supervise shutdown` and `estate quiesce` share it.

    `before_stop` runs once the lease is held and before the request is sent. `on_refusal` gets
    the lease (incarnation, token) of a refused request while the connection is open. The
    caller closes `conn`; `connect` opens the one reconnect."""
    from dsl41.runner_adapters import SupervisorConn
    from dsl41.runner_clock import EngineError
    from dsl41.runner_procid import current_boot_id, valid_start_token
    from dsl41.runner_supervisor import MAINTENANCE_STOP_VERSION

    maintenance_attempted = False
    reconnect = connect or SupervisorConn
    probe = ping_probe(sock_path, reconnect)
    try:
        ping = conn.send({"cmd": "PING"})
        if ping.get("ok") is True and ping.get("process_state") in _OPERATION_STATES:
            # an operation already admitted, by a client lost or gone: watch it, send nothing;
            # from here a failure is an unknown outcome, never a refusal
            maintenance_attempted = True
            code, reason, record = settle_maintenance(
                sock_path.parent, ping.get("incarnation"), sleep=sleep, probe=probe
            )
            return _settled(code, reason, record, sock_path)
        if ping.get("ok") is True and ping.get("process_state") == "draining":
            return refuse(DRAINING, prefix=f"supervisor {sock_path}"), {
                "ok": False,
                "error": "draining",
            }
        acq = conn.send({"cmd": "ACQUIRE", "controller_id": controller_id, "ttl_s": 60})
        if not acq.get("ok"):
            return refuse(f"cannot acquire lease: {json.dumps(acq, sort_keys=True)}"), acq
        incarnation = acq.get("incarnation")
        token = acq.get("token")
        if not isinstance(incarnation, str) or not incarnation or type(token) is not int:
            raise ValueError("malformed supervisor lease reply")
        if before_stop is not None:
            before_stop()
        # A send failure can follow a partial or complete delivery (DL-216, DL-390).
        maintenance_attempted = True
        request = {
            "cmd": "MAINTENANCE_STOP",
            "v": MAINTENANCE_STOP_VERSION,
            "token": token,
            "force": force,
        }
        try:
            resp = conn.send(request)
        except (OSError, ValueError, EngineError) as exc:
            typer.echo(f"supervisor {sock_path}: the reply was lost ({exc}); resolving", err=True)
            code, reason, record = _lost_reply(
                sock_path,
                reconnect,
                controller_id=controller_id,
                incarnation=incarnation,
                token=token,
                sleep=sleep,
            )
        else:
            if resp.get("ok") is False and resp.get("error") == "draining":
                # the drain began after the PING; nothing was admitted (DL-416)
                if on_refusal is not None:
                    on_refusal(incarnation, token)
                return refuse(DRAINING, prefix=f"supervisor {sock_path}"), resp
            if resp.get("ok") is False and resp.get("error") in _MAINTENANCE_REFUSALS:
                if on_refusal is not None:
                    on_refusal(incarnation, token)
                return refuse(json.dumps(resp, sort_keys=True)), resp
            if resp.get("ok") is False and resp.get("error") == "maintenance_in_progress":
                # another request's operation runs: watch it, send nothing more
                code, reason, record = settle_maintenance(
                    sock_path.parent, incarnation, sleep=sleep, probe=probe
                )
            else:
                bound = resp.get("bound_s")
                if not (
                    resp.get("ok") is True
                    and resp.get("maintenance") == "admitted"
                    and resp.get("incarnation") == incarnation
                    and resp.get("boot_id") == current_boot_id()
                    and type(resp.get("supervisor_pid")) is int
                    and valid_start_token(resp.get("supervisor_start_time"))
                    and isinstance(bound, int | float)
                    and not isinstance(bound, bool)
                    and 0 <= bound < float("inf")
                ):
                    raise ValueError(
                        f"maintenance admission is unknown: {json.dumps(resp, sort_keys=True)}"
                    )
                code, reason, record = settle_maintenance(
                    sock_path.parent,
                    incarnation,
                    within_s=float(bound) + MAINTENANCE_MARGIN_S,
                    sleep=sleep,
                    probe=probe,
                )
        return _settled(code, reason, record, sock_path)
    except (OSError, ValueError, EngineError) as exc:
        refuse(exc, prefix=f"supervisor {sock_path}")
        return (4 if maintenance_attempted else 2), None


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
