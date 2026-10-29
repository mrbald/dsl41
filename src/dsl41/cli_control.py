"""The control-plane verbs: what an operator says to a RUNNING engine (DL-137).

`sendevent`, `host` and `query` speak the ss10 control protocol over the run root's socket
(docs/control-protocol.md); `ui` and `serve` attach the ss11 TUI to it; `supervise` speaks the
Tier-1 supervisor's protocol (docs/supervisor-protocol.md). `supervise start` execs that
supervisor; the other verbs are clients and hold no engine state.

The four mutation exit codes (0/2/3/4) are DL-92's and are read in `cli_common.command_outcome`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from dsl41.cli_common import (
    command_outcome,
    guarded_maintenance,
    settle_rerun,
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
    """`sendevent`'s and `host`'s exit: one concurrency-model ss6 command envelope, its outcome as
    this process's status. The ladder is `cli_common.command_outcome` (DL-92, DL-137)."""
    raise typer.Exit(command_outcome(socket_path, request))


_SOCKET_OPT = typer.Option(
    ...,
    "--socket",
    "-S",
    help="The engine's control socket, <run-root>/control.sock.",
)


_EPOCH_PIN_HELP = (
    "Use this epoch instead of reading it. An exact retry needs the epoch"
    " the original carried; the CLI prints it before sending."
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
    socket_path: Path, key: str, expect: int | None, epoch: int | None, baseline: str | None
) -> tuple[str, int, int]:
    """The concurrency-model ss6 read header (`baseline_id`, `epoch`) and the current revision
    of `key`, with each pin replacing its own read value (DL-217).

    The fingerprint covers the revision, the epoch and the baseline, so an id carried back alone
    after any of them moved is a collision, not a retry. Unpinned values are read as before. The
    read narrows the race to one round trip and cannot remove it; whoever looked at a status page
    and then acts should pass --expect with the revision they looked at."""
    from dsl41.runner_control import read_for, revision_in

    response = _control_roundtrip(socket_path, read_for(key))
    header = read_header_of(response)
    if header is None:
        raise typer.Exit(2)
    read_baseline, read_epoch = header
    current = revision_in(response, key)
    return (
        read_baseline if baseline is None else baseline,
        read_epoch if epoch is None else epoch,
        current if expect is None else expect,
    )


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
        " only safe answer to exit 4. Give it with the --expect, --epoch"
        " and --baseline the CLI printed; other values make a different"
        " request, which is refused. Without it a fresh id is generated.",
    ),
    epoch: int = typer.Option(None, "--epoch", help=_EPOCH_PIN_HELP),
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
         the same arguments with the --request-id, --expect, --epoch
         and --baseline printed on stderr.

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
    baseline_id, epoch_value, revision = _pinned_read(socket_path, key, expect, epoch, baseline)
    request = command(
        verb,
        payload,
        key=key,
        revision=revision,
        baseline_id=baseline_id,
        epoch=epoch_value,
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
    baseline, epoch = header
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
            epoch=epoch,
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


def host(
    action: str = typer.Argument(..., metavar="ACTION", help="list, drain, activate or evict."),
    host_id: str = typer.Argument(
        None, metavar="HOST_ID", help="The host id. Needed by every action but list."
    ),
    socket_path: Path = _SOCKET_OPT,
    force: bool = typer.Option(
        False,
        "--force",
        help="evict only: skip the preconditions. Recorded with the actor"
        " who claimed it. This is the one path that can run a job twice,"
        " so use it only with proof from outside that the machine is"
        " dead.",
    ),  # ss8
    expect: int = typer.Option(
        None,
        "--expect",
        help="The state_rev you read for the host with 'host list'. The"
        " command is rejected if the host moved since. Without it, the"
        " command reads the revision first.",
    ),
    request_id: str = typer.Option(
        None,
        "--request-id",
        help="Retry the command that carried this id. See 'dsl41 sendevent --help'.",
    ),
    epoch: int = typer.Option(None, "--epoch", help=_EPOCH_PIN_HELP),
    baseline: str = typer.Option(None, "--baseline", help=_BASELINE_PIN_HELP),
    actor: str = typer.Option(None, "--claimed-actor", help=_ACTOR_PIN_HELP),
) -> None:
    """List, drain, activate or evict execution hosts.

    ACTION is one of:
      list      Show the routing table with each host's revision.
      drain     Stop routing new work to the host. Running work finishes.
                Reversible; use it for planned maintenance.
      activate  Route work to the host again and dispatch what the drain held.
      evict     Declare the host's work reroutable to other hosts. Refused
                unless the leader has recorded the host unreachable, the host
                runs a deadman, and the kill bound has passed.

    drain, activate and evict use sendevent's exit codes: 0 applied, 2
    refused, 3 rejected, 4 unknown.
    """
    # Design: concurrency-model ss8
    from dsl41.oracle_state import RuntimeState
    from dsl41.runner_control import claimed_actor, command

    verb = action.lower()
    if verb == "list":
        _answer_or_exit(_control_roundtrip(socket_path, {"cmd": "hosts"}))
    if not host_id:
        raise typer.Exit(refuse(f"`host {verb}` needs a host id"))
    key = RuntimeState.host_key(host_id)
    baseline_id, epoch_value, revision = _pinned_read(socket_path, key, expect, epoch, baseline)
    request = command(
        verb,
        {"id": host_id, "force": force},
        key=key,
        revision=revision,
        baseline_id=baseline_id,
        epoch=epoch_value,
        request_id=request_id,
        claimed_actor=claimed_actor() if actor is None else actor,
        cmd="host",
    )
    _mutate(socket_path, request)


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


#: a READ verb this surface does NOT forward: `dsl41 control host list` owns `hosts`.
_QUERY_ELSEWHERE: frozenset[str] = frozenset({"hosts"})

#: the read verbs this surface forwards, derived from `runner_access.REQUIRED_TIER` (DL-152); the
#: argument's HELP is derived from them (DL-145).
_QUERY_VERBS: tuple[str, ...] = tuple(
    verb
    for verb, tier in REQUIRED_TIER.items()
    if tier is Tier.READ and verb not in _QUERY_ELSEWHERE
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
                    # the skim carries the revision because --expect is honest only when it
                    # names what the operator LOOKED at
                    f" rev {row.get('state_rev', 0):<4} {flags}".rstrip()
                )
            if response.get("spec_drift"):
                typer.echo("SPEC DRIFT: estate files changed on disk", err=True)
            raise typer.Exit(0)
        _answer_or_exit(response)
    _stream_subscribe(socket_path, request)


def supervise(
    action: str = typer.Argument(..., metavar="ACTION", help="start, list or shutdown."),
    run_root: Path = typer.Option(
        ..., "--run-root", help="Run directory holding supervisor.sock."
    ),  # ss6a Tier 1
    deadman_seconds: float | None = typer.Option(
        None,
        "--deadman-seconds",
        help="start only: exit after this many seconds without a holder.",
    ),
    force: str | None = typer.Option(
        None, "--force", help="shutdown only: terminate known command groups deliberately."
    ),
) -> None:
    """Start, list or request guarded maintenance of a supervisor.

    shutdown refuses active or unobservable work by default. --force=terminate
    uses TERM, each command's grace period and KILL on validated command groups.
    Orphaning is unsupported. The supervisor answers at once and the CLI
    watches the operation in maintenance.json. Shutdown exits 0 on verified
    completion, 2 on refusal, 3 when the operation still runs as the wait ends
    (it finishes on its own) and 4 when the outcome is unknown; run again, it
    watches an operation already admitted and sends nothing. start returns 8 on
    maintenance completion, 0 on host shutdown, 1 on failure, 2 on bad
    configuration and 7 on a statechart error.
    """
    # Design: runner-design ss6a, DL-42 item 4, DL-210, DL-401
    import math
    import os
    import sys

    from dsl41.runner_adapters import SupervisorConn, supervisor_argv, supervisor_log_path
    from dsl41.runner_clock import EngineError

    verb = action.lower()
    if verb not in ("start", "list", "shutdown"):
        raise typer.Exit(refuse(f"unknown supervise action {action!r} (start|list|shutdown)"))
    if not isinstance(force, str | type(None)):
        force = None  # direct Python callers omit Typer's Option default
    if force is not None and (verb != "shutdown" or force != "terminate"):
        raise typer.Exit(refuse("--force=terminate is valid only for supervise shutdown"))
    if deadman_seconds is not None:
        if verb != "start":
            raise typer.Exit(refuse("--deadman-seconds is only valid for supervise start"))
        if not (math.isfinite(deadman_seconds) and deadman_seconds > 0):
            raise typer.Exit(refuse("--deadman-seconds must be a finite positive number"))
    if verb == "start":
        try:
            try:
                run_root.mkdir(mode=0o700, parents=True)
            except FileExistsError:
                if not run_root.is_dir():
                    raise
            else:
                run_root.chmod(0o700)
            argv = supervisor_argv(run_root.resolve(), deadman_seconds)
            log_fd = os.open(
                supervisor_log_path(run_root), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            try:
                os.dup2(log_fd, 1)
                os.dup2(log_fd, 2)
            finally:
                if log_fd > 2:
                    os.close(log_fd)
            null_fd = os.open(os.devnull, os.O_RDONLY)
            try:
                os.dup2(null_fd, 0)
            finally:
                if null_fd > 2:
                    os.close(null_fd)
            # dup2(fd, fd) leaves O_CLOEXEC intact when open reused stdio.
            for fd in (0, 1, 2):
                os.set_inheritable(fd, True)
            os.execv(sys.executable, argv)
        except OSError as exc:
            raise typer.Exit(refuse(exc, prefix="supervisor start")) from exc
    sock_path = run_root / "supervisor.sock"
    if not sock_path.exists():
        if verb == "shutdown" and (run_root / "maintenance.json").exists():
            # a rerun after an admitted operation: its record says how it ended (DL-401)
            raise typer.Exit(settle_rerun(run_root))
        raise typer.Exit(refuse(f"no supervisor at {sock_path}"))
    try:
        conn = SupervisorConn(sock_path)
    except OSError as exc:
        raise typer.Exit(refuse(exc, prefix=f"supervisor {sock_path}")) from exc
    try:
        if verb == "list":
            try:
                listing = conn.send({"cmd": "LIST"})
            except (OSError, ValueError, EngineError) as exc:
                raise typer.Exit(refuse(exc, prefix=f"supervisor {sock_path}")) from exc
            _answer_or_exit(listing)
        code, _ = guarded_maintenance(
            conn, sock_path, controller_id=f"supervise-cli-{os.getpid()}", force=force
        )
    finally:
        conn.close()
    raise typer.Exit(code)
