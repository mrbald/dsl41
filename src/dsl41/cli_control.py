"""The control-plane verbs: what an operator says to a RUNNING engine (DL-137).

`sendevent` and `query` speak the ss10 control protocol over the run root's socket
(docs/control-protocol.md); `ui` and `serve` attach the ss11 TUI to it. Every verb is a client
and holds no engine state.

The four mutation exit codes (0/2/3/4) are DL-92's and are read in `cli_common.command_outcome`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from dsl41.cli_common import (
    command_outcome,
    import_tui_or_exit_2,
    read_header_of,
    refuse,
)
from dsl41.oracle_state import FAILED
from dsl41.runner_access import REQUIRED_TIER, Tier


# ------------------------------------------------------------ the control plane
#
# Exit codes: 0 a clean answer (an `ok` response), 2 the command never reached the engine
# (unreachable socket, unreadable answer, refusal). `run`'s 1 belongs to the engine. `off-hold-all`
# is the one aggregate: its 1 means "some per-job decision was not applied", with the detail on the
# per-job lines (DL-181).
#
# `sendevent` splits 2 further (S3), because a command can fail in three ways that call for three
# next moves (DL-92): 2 is REFUSED (nothing admitted, nothing logged), 3 is REJECTED (a decision,
# with an index; the world moved) and 4 is UNKNOWN (no decision arrived; it may yet apply). No
# other verb uses 3 or 4.


def _control_roundtrip(socket_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Exit-code shell around runner_control.roundtrip (DL-78): a failed READ is exit 2.

    Reads only; a MUTATION takes `cli_common.command_outcome`."""
    from dsl41.runner_control import ControlClientError, roundtrip

    try:
        return roundtrip(socket_path, request)
    except ControlClientError as exc:
        raise typer.Exit(refuse(exc)) from exc


def _answer_or_exit(response: dict[str, Any]) -> None:
    """Print one control answer as compact JSON (sort_keys, no indent) and exit on its `ok`
    flag (DL-178h)."""
    import json as json_mod

    typer.echo(json_mod.dumps(response, sort_keys=True))
    raise typer.Exit(0 if response.get("ok") else 2)


def _mutate(socket_path: Path, request: dict[str, Any]) -> None:
    """`sendevent`'s exit: one concurrency-model ss6 command envelope, its outcome as this
    process's status. The ladder is `cli_common.command_outcome` (DL-92, DL-137)."""
    raise typer.Exit(command_outcome(socket_path, request))


_SOCKET_OPT = typer.Option(
    ...,
    "--socket",
    "-S",
    help="The engine's control socket, <run-root>/control.sock.",
)


_BASELINE_PIN_HELP = (
    "Use this baseline id instead of reading it. An exact retry needs the"
    " baseline the original carried; the CLI prints it before sending."
)
_ACTOR_PIN_HELP = (
    "Reuse the original claimed actor for exact recovery. This is logged metadata;"
    " the socket still authenticates the current OS principal."
)


def _pinned_read(
    socket_path: Path, key: str, expect: int | None, baseline: str | None
) -> tuple[str, int]:
    """The concurrency-model ss6 read header's `baseline_id` and the current revision of `key`,
    with each pin replacing its own read value (DL-217).

    The fingerprint covers the revision and the baseline, so an id carried back alone after
    either moved is a collision, not a retry. Unpinned values are read as before. The
    read narrows the race to one round trip and cannot remove it; whoever looked at a status page
    and then acts should pass --expect with the revision they looked at."""
    from dsl41.runner_control import read_for, revision_in

    response = _control_roundtrip(socket_path, read_for(key))
    header = read_header_of(response)
    if header is None:
        raise typer.Exit(2)
    current = revision_in(response, key)
    return (header if baseline is None else baseline, current if expect is None else expect)


def sendevent(
    event: str = typer.Argument(
        ...,
        metavar="EVENT",
        help="One of STARTJOB, FORCE_STARTJOB, KILLJOB, ON_ICE, OFF_ICE,"
        " ON_HOLD, OFF_HOLD, ON_NOEXEC, OFF_NOEXEC, DISARM, RELEASE_RESOURCE,"
        " SET_GLOBAL, CHANGE_STATUS. DISARM clears the job's armed latch and"
        " does nothing else. RELEASE_RESOURCE frees the resource units a job"
        " still holds after its run ended.",
    ),
    socket_path: Path = _SOCKET_OPT,
    job: str = typer.Option(
        None, "--job", "-J", help="Target job. Needed by the job events and CHANGE_STATUS."
    ),
    status: str = typer.Option(None, "--status", "-s", help="CHANGE_STATUS: the new status."),
    global_kv: str = typer.Option(None, "--global", "-G", help="SET_GLOBAL: NAME=value."),
    exit_code: int = typer.Option(None, "--exit-code", help="CHANGE_STATUS: exit code to record."),
    expect: int = typer.Option(
        None,
        "--expect",
        help="The state_rev you read for the target with 'query status' or"
        " 'query global'. The event is rejected if the target moved since."
        " Without it, the command reads the revision first, which narrows"
        " the race to one round trip. For SET_GLOBAL, 0 means the global"
        " must not exist yet.",
    ),
    request_id: str = typer.Option(
        None,
        "--request-id",
        help="Retry the event that carried this id instead of sending a"
        " new one. An exact retry, same id and same values, is answered"
        " from the original decision and applies nothing twice. It is the"
        " only safe answer to exit 4. Give it with the --expect and"
        " --baseline the CLI printed; other values make a different"
        " request, which is refused. Without it a fresh id is generated.",
    ),
    baseline: str = typer.Option(None, "--baseline", help=_BASELINE_PIN_HELP),
    actor: str = typer.Option(None, "--claimed-actor", help=_ACTOR_PIN_HELP),
) -> None:
    """Send an AutoSys-style event to a running engine.

    Every event names the revision it was composed against, and the
    engine answers with one of four decisions, each with its own exit
    code:

      0  Applied.
      2  Refused. The request was not admitted and nothing was logged.
         Fix it and send it again. A refused retry says nothing about
         the original it retries: that one may have applied, and stderr
         prints what it decided.
      3  Rejected. The target changed between your read and the write.
         The rejection is in the log. Re-read, then decide again;
         resending the same request loses the same race.
      4  Unknown. No decision arrived. The event may still be admitted
         and about to apply. Re-read. If you must send it again, repeat
         the same arguments with the --request-id, --expect and
         --baseline printed on stderr.

    Before the first write, one stderr line prints those retry values.
    Keep it: with the original arguments, it is everything a safe retry
    needs.
    """
    # Design: runner-design ss10, concurrency-model ss6, control-protocol
    # ss3, DL-217
    from dsl41.runner_admission import addressed_key
    from dsl41.runner_clock import EngineError
    from dsl41.runner_control import claimed_actor, command

    verb = event.upper()
    payload: dict[str, Any] = {}
    if job is not None:
        payload["job"] = job
    if status is not None:
        payload["status"] = status.upper()
    if global_kv is not None:
        name, sep, value = global_kv.partition("=")
        if not sep or not name:
            raise typer.Exit(refuse('--global expects "NAME=value"'))
        payload["name"], payload["value"] = name, value
    if exit_code is not None:
        payload["exit_code"] = exit_code
    try:
        key = addressed_key(verb, payload)
    except EngineError as exc:
        raise typer.Exit(refuse(exc)) from exc
    baseline_id, revision = _pinned_read(socket_path, key, expect, baseline)
    request = command(
        verb,
        payload,
        key=key,
        revision=revision,
        baseline_id=baseline_id,
        request_id=request_id,
        claimed_actor=claimed_actor() if actor is None else actor,
    )
    _mutate(socket_path, request)


def off_hold_all(
    socket_path: Path = _SOCKET_OPT,
    dry_run: bool = typer.Option(False, "--dry-run", help="List the held jobs and send nothing."),
) -> None:
    """Take every held job off hold in one sweep.

    One status read selects the held jobs and supplies the revision
    each OFF_HOLD is composed against. A job that changed in between is
    rejected with its own printed decision. Each release re-evaluates
    the job's start, as a single OFF_HOLD does.

    Each job gets a fresh request id, so the sweep as a whole cannot be
    retried. The pre-send line and any exit-4 advice name each job's
    exact retry as a single 'sendevent OFF_HOLD --job <name>' with that
    job's values.

    Exit codes: 0 every release applied; 1 some per-job decision did not
    apply (each job's answer is printed with sendevent's 0, 2, 3 or 4
    meaning); 2 the status read failed and nothing was sent.
    """
    # Design: DL-180, control-protocol ss3, DL-158, SEM-21, DL-217
    import shlex

    from dsl41.runner_admission import addressed_key
    from dsl41.runner_control import claimed_actor, command, revision_in

    response = _control_roundtrip(socket_path, {"cmd": "status"})
    if not response.get("ok"):
        raise typer.Exit(refuse(str(response.get("error", "status query failed"))))
    header = read_header_of(response)
    if header is None:
        raise typer.Exit(2)
    baseline = header
    rows = response.get("jobs", {})
    # on_hold is the job-verb latch, NOT DL-94's held run-state in the same row; the sweep is named
    # for the event it sends and its scope (DL-423)
    held = sorted(name for name, row in rows.items() if row.get("on_hold"))
    if not held:
        typer.echo("no jobs held")
        raise typer.Exit(0)
    if dry_run:
        for name in held:
            typer.echo(name)
        raise typer.Exit(0)
    all_applied = True
    for name in held:
        payload = {"job": name}
        key = addressed_key("OFF_HOLD", payload)
        request = command(
            "OFF_HOLD",
            payload,
            key=key,
            revision=revision_in(response, key),
            baseline_id=baseline,
            request_id=None,
            claimed_actor=claimed_actor(),
        )
        typer.echo(f"-- {name}")
        retry_command = (
            f"sendevent OFF_HOLD --job {shlex.quote(name)} --socket {shlex.quote(str(socket_path))}"
        )
        if command_outcome(socket_path, request, retry_command=retry_command) != 0:
            all_applied = False
    raise typer.Exit(0 if all_applied else 1)


#: how often `engine stop --wait` looks for the engine's end
STOP_POLL_S = 0.2

engine_app = typer.Typer(
    no_args_is_help=True,
    help="Act on the engine process itself.",
    context_settings={"help_option_names": ["-h", "--help"]},
)


@engine_app.command("stop")
def engine_stop(
    socket_path: Path = _SOCKET_OPT,
    wait: bool = typer.Option(
        False,
        "--wait",
        help="Return only once the engine has exited: its socket is gone and its run root's"
        " leader.lock is free. An engine still starting (the lock held, no socket yet) gets"
        " the stop as soon as it serves; one not running (the lock free) counts as stopped.",
    ),
) -> None:
    """Drain the running engine, as SIGTERM does.

    The engine stops admitting mutations (they are refused with
    engine_draining), asks every running job to stop (TERM, then KILL
    after the grace), records how each ended, and exits 0. A second
    stop moves the drain's deadline to now. This is the systemd unit's
    ExecStop, with --wait. It needs the adm tier when an access map is
    armed.

    Exit codes: 0 the engine took the stop (with --wait: and exited, or
    no engine holds the run root); 2 the engine refused it or could not
    be reached.
    """
    # Design: spec-drain, docs/control-protocol.md ss2
    import json as json_mod

    from dsl41.runner_control import ControlClientError, roundtrip

    try:
        response = roundtrip(socket_path, {"cmd": "engine_stop"})
    except ControlClientError as exc:
        if not wait or exc.delivered:
            raise typer.Exit(refuse(exc)) from exc
        retried = _stop_once_reachable(socket_path)
        if retried is None:
            typer.echo(f"engine not running: {exc}")
            return
        response = retried
    typer.echo(json_mod.dumps(response, sort_keys=True))
    if not response.get("ok"):
        raise typer.Exit(2)
    if wait:
        _wait_for_exit(socket_path)


def _stop_once_reachable(socket_path: Path) -> dict[str, Any] | None:
    """`--wait` with no socket to answer: a free `leader.lock` means no engine, so None; a held
    one means an engine still starting, so the stop is sent again every poll until it is
    delivered. A send that may have arrived is an unknown outcome and exits 2."""
    import time

    from dsl41.runner_control import ControlClientError, roundtrip

    while _lock_held(socket_path.parent / "leader.lock"):
        time.sleep(STOP_POLL_S)
        try:
            return roundtrip(socket_path, {"cmd": "engine_stop"})
        except ControlClientError as exc:
            if exc.delivered:
                raise typer.Exit(refuse(exc)) from exc
    return None


def _wait_for_exit(socket_path: Path) -> None:
    """Block until the engine behind `socket_path` has exited: the socket refuses connects,
    then the run root's `leader.lock` is free. The engine releases the lock last."""
    import time

    while _answers(socket_path):
        time.sleep(STOP_POLL_S)
    while _lock_held(socket_path.parent / "leader.lock"):
        time.sleep(STOP_POLL_S)


def _answers(socket_path: Path) -> bool:
    import socket as socket_mod

    with socket_mod.socket(socket_mod.AF_UNIX) as probe:
        try:
            probe.connect(str(socket_path))
        except OSError:
            return False
    return True


def _lock_held(path: Path) -> bool:
    """Whether another process holds `path`'s flock. A shared probe, released at once, takes
    nothing from a waiting engine."""
    import fcntl
    import os

    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def ui(socket_path: Path = _SOCKET_OPT) -> None:
    """Open the terminal UI against a running engine.

    The UI shows the jobs table, an explain pane with each condition
    atom's truth, the log tail and a sendevent console. It is a client
    of the control socket only: quitting detaches the viewer and leaves
    the run alone. 'run --ui' is different; there the terminal owns the
    run.

    Exit codes: 0 the viewer quit; 2 the ui extra is not installed or
    the socket does not exist.
    """
    # Design: ss11
    runner_tui = import_tui_or_exit_2()
    if not socket_path.exists():
        raise typer.Exit(refuse(f"control socket {socket_path}: no such file"))
    app = runner_tui.RunnerApp(socket_path)
    app.run()
    if app.return_code:
        # textual's fatal-error path returns normally with return_code set
        # (app.py _handle_exception): a crashed TUI must not exit 0
        raise typer.Exit(app.return_code)


def _import_textual_serve_or_exit_2():
    """Guarded textual-serve import (runner-design ss11/ss14): textual-serve is the [ui] extra's
    other half and spawns one app subprocess per browser session."""
    try:
        from textual_serve.server import Server
    except ModuleNotFoundError as exc:
        raise typer.Exit(
            refuse("`serve` needs the optional [ui] extra: pip install 'dsl41[ui]'")
        ) from exc
    return Server


def serve(
    socket_path: Path = _SOCKET_OPT,
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address. Keep the loopback default unless a proxy or"
        " tunnel provides authentication.",
    ),  # ss11
    port: int = typer.Option(8000, "--port", help="Bind port."),
) -> None:
    """Serve the terminal UI in a web browser.

    Each browser session gets its own 'dsl41 ui' process against the
    same running engine. The server has no authentication of its own.
    Keep it on loopback, or put a proxy or tunnel in front; see the
    README's deployment notes.

    Exit codes: 2 the ui extra is not installed, the socket does not
    exist, or the address cannot be bound. Otherwise it serves until
    interrupted.
    """
    # Design: ss11
    import shlex
    import sys

    server_cls = _import_textual_serve_or_exit_2()
    if not socket_path.exists():
        raise typer.Exit(refuse(f"control socket {socket_path}: no such file"))
    command = f"{shlex.quote(sys.executable)} -m dsl41 ui --socket {shlex.quote(str(socket_path))}"
    try:
        server_cls(command, host=host, port=port).serve()
    except OSError as exc:
        raise typer.Exit(refuse(exc, prefix=f"serve {host}:{port}")) from exc


def _brief_flags(row: dict[str, object]) -> str:
    """The --brief flags column, I/H/N/A in the TUI's fixed order, from the same tuple (DL-68,
    DL-145)."""
    from dsl41.runner_control import STATUS_FLAG_MARKS

    return "".join(mark for mark, key in STATUS_FLAG_MARKS if row.get(key))


#: the read verbs this surface forwards, derived from `runner_access.REQUIRED_TIER` (DL-152); the
#: argument's HELP is derived from them (DL-145).
_QUERY_VERBS: tuple[str, ...] = tuple(
    verb for verb, tier in REQUIRED_TIER.items() if tier is Tier.READ
)
_QUERY_PREDICATES: dict[str, frozenset[str]] = {
    "is-success": frozenset({"SUCCESS"}),
    "is-failed": FAILED,
}


def _stream_subscribe(socket_path: Path, request: dict[str, Any]) -> None:
    """`query subscribe`: print the ack, then journal records, until the engine hangs up or the
    operator interrupts.

    Presentation only (DL-172): `runner_control.subscribe_lines` owns the protocol, including the
    DL-151 rule that a refusal ack does NOT close the connection. `ControlClientError` is the
    engine's own refusal text, printed as-is; `OSError` is a transport failure, prefixed with the
    socket (DL-78).

    The stream ends at the operator's interrupt (exit 0), or at the engine (exit 2): a hangup, or
    an answer line such as a backfill refusal (control-protocol ss5, DL-267). Exit 2 names the
    `--since` to resume from: the last `seq` printed, or the ack's cursor when none was."""
    from dsl41.runner_control import (
        ControlClientError,
        StreamLineTooLong,
        resume_cursor,
        subscribe_lines,
    )

    cursor: int | None = None
    streaming = False  # the first line is the ack: a refusal before it is not an end
    try:
        for line in subscribe_lines(socket_path, request):
            typer.echo(line)
            streaming = True
            cursor = resume_cursor(line, cursor)
    except StreamLineTooLong as exc:
        raise typer.Exit(refuse(exc)) from exc  # no hint: the same cursor meets it again
    except ControlClientError as exc:
        if not streaming:
            raise typer.Exit(refuse(exc)) from exc
        raise typer.Exit(refuse(f"{exc} ({_resume_hint(cursor)})")) from exc
    except OSError as exc:
        why = f"{exc} ({_resume_hint(cursor)})" if streaming else exc
        raise typer.Exit(refuse(why, prefix=f"control socket {socket_path}")) from exc
    except KeyboardInterrupt:
        return
    raise typer.Exit(refuse(f"the engine closed the subscribe stream ({_resume_hint(cursor)})"))


def _resume_hint(cursor: int | None) -> str:
    if cursor is None:  # an engine that sent no cursor: the reader's own is all there is
        return "resubscribe with --since set to the last seq you read"
    return f"resubscribe with --since {cursor}"


def query(
    what: str = typer.Argument(
        ...,
        metavar="WHAT",
        help="One of " + ", ".join([*_QUERY_VERBS, *_QUERY_PREDICATES]) + ".",
    ),
    socket_path: Path = _SOCKET_OPT,
    job: str = typer.Option(
        None,
        "--job",
        "-J",
        help="status: only this job. explain, spec, deps, is-success, is-failed: the job to query.",
    ),
    name: list[str] = typer.Option(
        None, "--name", "-N", help="global, globals: the global to read. Repeatable."
    ),
    since: int = typer.Option(
        None, "--since", help="trace, subscribe: only records after this sequence number."
    ),
    brief: bool = typer.Option(
        False,
        "--brief",
        help="status: one line per job (name, status, at, run, exit, flags,"
        " rev) instead of the JSON document.",
    ),  # DL-66
) -> None:
    """Read status, traces, explanations and more from a running engine.

    The queries are listed under WHAT below. subscribe streams journal
    records as JSON lines until interrupted, and exits 2 if the engine
    closes the stream first; resubscribe with --since.

    is-success and is-failed print the job's current status and exit 0
    when it matches (SUCCESS for is-success; FAILURE or TERMINATED for
    is-failed), and 1 when it does not. They are for scripts, like
    'systemctl is-active'.

    status, global and globals report the state_rev of each named
    entity. That is the value 'sendevent --expect' is composed from.
    global reports an unset name at revision 0 rather than leaving it
    out.

    Exit codes: 0 answered; 1 is-success or is-failed did not match; 2 an
    unknown query, a missing option, an engine that refused or could
    not be reached, or a subscribe stream the engine closed.
    """
    # Design: runner-design ss10, ss11, DL-65, concurrency-model ss6
    verb = what.lower()
    known, predicates = _QUERY_VERBS, _QUERY_PREDICATES
    if verb not in known and verb not in predicates:
        raise typer.Exit(refuse(f"unknown query {what!r} ({'|'.join([*known, *predicates])})"))
    if verb in predicates:
        if job is None:
            raise typer.Exit(refuse(f"{verb} requires --job"))
        response = _control_roundtrip(socket_path, {"cmd": "status", "job": job})
        if not response.get("ok"):
            raise typer.Exit(refuse(str(response.get("error", "status query failed"))))
        current = response["jobs"][job]["status"]
        typer.echo(current)
        raise typer.Exit(0 if current in predicates[verb] else 1)
    if brief and verb != "status":
        raise typer.Exit(refuse("--brief applies to status only"))
    if verb in ("global", "globals") and not name:
        raise typer.Exit(refuse(f"{verb} requires --name (repeat it for several)"))
    if verb == "global" and len(name or ()) > 1:
        raise typer.Exit(refuse("global names one; use `globals` for several"))
    request: dict[str, Any] = {"cmd": verb}
    if job is not None:
        request["job"] = job
    if since is not None:
        request["since"] = since
    if name:
        # one verb per shape, as the server has them: `global` names one and `globals` a list,
        # so a client wanting a single revision need not unwrap a map
        request.update({"name": name[0]} if verb == "global" else {"names": list(name)})
    if verb != "subscribe":
        from dsl41.runner_control import status_label

        response = _control_roundtrip(socket_path, request)
        if brief and response.get("ok"):
            # the status field is as wide as its widest label, so an attempt beside a status
            # ("STOPPING (engine_stop)") keeps the columns after it aligned (DL-426)
            rows = response.get("jobs", {})
            width = max([10, *(len(status_label(row)) for row in rows.values())])
            for job_name in sorted(rows):
                row = rows[job_name]
                flags = _brief_flags(row)
                exit_code = row.get("exit_code")
                typer.echo(
                    f"{job_name:<44} {status_label(row):<{width}}"
                    f" {(row.get('status_at') or '-'):<26}"
                    f" run {row.get('run_number', 0):<4}"
                    f" exit {'-' if exit_code is None else exit_code:<4}"
                    # the skim carries the revision because --expect is honest only when it
                    # names what the operator LOOKED at
                    f" rev {row.get('state_rev', 0):<4} {flags}".rstrip()
                )
            if response.get("spec_drift"):
                typer.echo("SPEC DRIFT: estate files changed on disk", err=True)
            raise typer.Exit(0)
        _answer_or_exit(response)
    _stream_subscribe(socket_path, request)
