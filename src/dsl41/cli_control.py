"""The control-plane verbs: what an operator says to a RUNNING engine
(DL-137's split).

`sendevent`, `host` and `query` speak the ss10 control protocol over the
run root's socket (docs/control-protocol.md); `ui` and `serve` attach the
ss11 TUI to it; `supervise` speaks the other protocol, the Tier-1
supervisor's (docs/supervisor-protocol.md). `supervise start` execs that
supervisor; the other verbs are clients. Nothing here holds engine state.
Registered on the app in `cli.py`.

The four mutation exit codes (0/2/3/4) are DL-92's and are read in one
place, `cli_common.command_outcome`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from dsl41.cli_common import command_outcome, import_tui_or_exit_2, read_header_of, refuse
from dsl41.runner_access import REQUIRED_TIER, Tier


# ------------------------------------------------------------ the control plane
#
# Exit codes: 0 a clean answer (an `ok` response), 2 the command never
# reached the engine (an unreachable socket, an unreadable answer, a
# refusal); `run`'s 1 -- the estate failed while running -- belongs to the
# engine and no single-command client here can see it. `release-held` is
# the one aggregate: its 1 means "some per-job decision was not applied",
# with the detail on the per-job lines (DL-181 era, arch-review
# 2026-08-28).
#
# `sendevent` splits 2 further, because since S3 a command can fail in three
# ways that call for three different next moves and a script that cannot tell
# them apart has to guess exactly where guessing costs most (DL-92): 2 stays
# REFUSED (nothing admitted, nothing logged -- the "never started" reading of
# 2, unchanged), 3 is REJECTED (a decision, with an index; the world moved)
# and 4 is UNKNOWN (no decision arrived; it may yet apply). 1 keeps its
# meaning and no other verb uses 3 or 4.


def _control_roundtrip(socket_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Exit-code shell around runner_control.roundtrip (DL-78): the protocol
    client raises, the CLI decides that a failed READ is exit 2.

    Reads only. A read that did not answer changed nothing whether or not it
    was delivered, so it has one outcome; a MUTATION has four, and takes
    `cli_common.command_outcome`."""
    from dsl41.runner_control import ControlClientError, roundtrip

    try:
        return roundtrip(socket_path, request)
    except ControlClientError as exc:
        raise typer.Exit(refuse(exc)) from exc


def _answer_or_exit(response: dict[str, Any]) -> None:
    """Print one control answer and exit on its `ok` flag: `host list`,
    `query`'s non-brief read, and `supervise list`/`shutdown` all did this
    by hand, two of them indented and two not (DL-178h).

    Compact JSON (sort_keys=True, no indent): the split was an even two and
    two, and the tie breaks toward this file's other JSON idiom --
    `_stream_subscribe` already reads the wire as one record per line."""
    import json as json_mod

    typer.echo(json_mod.dumps(response, sort_keys=True))
    raise typer.Exit(0 if response.get("ok") else 2)


def _mutate(socket_path: Path, request: dict[str, Any]) -> None:
    """`sendevent`'s and `host`'s exit: one ss6 command envelope, its
    outcome as this process's status.

    The ladder itself is `cli_common.command_outcome` -- one reading of an
    answer for every mutating verb, the live seal included (DL-92,
    DL-137). What is left here is the surface's own half: a verb EXITS on
    the code, where an async body that owes a teardown returns it."""
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


def _pinned_read(
    socket_path: Path, key: str, expect: int | None, epoch: int | None, baseline: str | None
) -> tuple[str, int, int]:
    """The ss6 read header (`baseline_id`, `epoch`) and the current revision
    of `key`, with each pin replacing its own read value (DL-217).

    The fingerprint covers the revision, the epoch and the baseline, and the
    read returns the CURRENT three. So an id carried back alone, after any of
    them moved, is a different command under a reused id -- a collision, not
    a retry. Each pin replaces one value independently; what is not pinned
    is read as before. The read narrows the race to one round trip; it does
    not remove it, and it cannot: the value of a precondition is that it
    names what the DECIDER saw, and a number this process fetched a
    millisecond ago is only a very recent guess about that. Whoever looked
    at a status page and then chose to act should pass --expect with the
    revision they looked at."""
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
        claimed_actor=claimed_actor(),
    )
    _mutate(socket_path, request)


def release_held(
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
    # on_hold is the job-verb latch, NOT DL-94's `held` run-state in the
    # same row; the name `release-held` stays (declined rename, DL-181)
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
        claimed_actor=claimed_actor(),
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
    """Guarded textual-serve import (runner-design ss11/ss14): the [ui]
    extra's other half -- textual-serve spawns one app subprocess per
    browser session, so it needs its own dependency, not just textual's."""
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
    """The --brief flags column, I/H/N/A in the TUI's fixed order -- both
    surfaces render the same alphabet from the same status payload (DL-68),
    and since DL-145 from the same tuple."""
    from dsl41.runner_control import STATUS_FLAG_MARKS

    return "".join(mark for mark, key in STATUS_FLAG_MARKS if row.get(key))


#: a READ verb this surface does NOT forward: `dsl41 control host list`
#: owns `hosts`, so the query surface would answer it a second time.
_QUERY_ELSEWHERE: frozenset[str] = frozenset({"hosts"})

#: the read verbs this surface forwards. Module-level so the argument's
#: HELP is derived from them (DL-145): the hand-typed help string it
#: replaced was a second spelling of this set, and a verb added to the gate
#: below reached the gate and not the help.
#:
#: DERIVED from the tier map (DL-152). `runner_access.REQUIRED_TIER` is the
#: closed table of verb -> tier, so which verbs are READ is already written
#: there; a hand-kept copy here drifted the moment a verb was added to one
#: and not the other. The only thing this file still states is the verb it
#: deliberately does not forward.
_QUERY_VERBS: tuple[str, ...] = tuple(
    verb
    for verb, tier in REQUIRED_TIER.items()
    if tier is Tier.READ and verb not in _QUERY_ELSEWHERE
)
_QUERY_PREDICATES: dict[str, tuple[str, ...]] = {
    "is-success": ("SUCCESS",),
    "is-failed": ("FAILURE", "TERMINATED"),
}


def _stream_subscribe(socket_path: Path, request: dict[str, Any]) -> None:
    """`query subscribe`: print the ack, then journal records, until the
    engine hangs up or the operator interrupts.

    Presentation only (DL-172): `runner_control.subscribe_lines` owns the
    protocol -- the stamped version, the bounded read, and the DL-151 rule
    that a refusal ack does NOT close the connection. Its two exception
    types keep the CLI's exit-code mapping distinguishable the way
    `_control_roundtrip` already reads `ControlClientError` (DL-78):
    `ControlClientError` is the engine's own refusal text, printed as-is;
    `OSError` is a transport failure, prefixed with the socket like every
    other client here.

    The stream ends at the operator's interrupt, exit 0, or at the engine,
    exit 2: a hangup, or an answer line such as a backfill refusal
    (control-protocol ss5, DL-267). Exit 2 names the `--since` to resume
    from: the last `seq` this command printed, or the ack's cursor when it
    printed none."""
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
) -> None:
    """Start, list or shut down a run root's job supervisor.

    ACTION is one of:
      start     Run the supervisor in the foreground.
      list      Show its live runs and lease. Read-only.
      shutdown  Take the lease, then stop every command (TERM, a grace period,
                KILL), record each outcome, and remove the socket and pidfile.
                Fails with the holder's details while an engine holds an
                unexpired lease.

    Exit codes: list and shutdown exit 0 on success and 2 when there is
    no supervisor or the lease could not be taken. start exits 0 on an
    orderly exit, 1 when another owner holds the root, and 2 on a
    configuration or usage error.
    """
    # Design: runner-design ss6a, DL-42 item 4, DL-210
    import json as json_mod
    import math
    import os
    import sys

    from dsl41.runner_adapters import SupervisorConn, supervisor_argv, supervisor_log_path

    verb = action.lower()
    if verb not in ("start", "list", "shutdown"):
        raise typer.Exit(refuse(f"unknown supervise action {action!r} (start|list|shutdown)"))
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
        raise typer.Exit(refuse(f"no supervisor at {sock_path}"))
    try:
        conn = SupervisorConn(sock_path)
    except OSError as exc:
        raise typer.Exit(refuse(exc, prefix=f"supervisor {sock_path}")) from exc
    try:
        if verb == "list":
            _answer_or_exit(conn.send({"cmd": "LIST"}))
        acq = conn.send(
            {"cmd": "ACQUIRE", "controller_id": f"supervise-cli-{os.getpid()}", "ttl_s": 60}
        )
        if not acq.get("ok"):
            raise typer.Exit(refuse(f"cannot acquire lease: {json_mod.dumps(acq, sort_keys=True)}"))
        # the token alone does not authorize a mutating verb: DL-80 pairs it
        # with the incarnation the same ACQUIRE reply names, and `conn` has
        # read that back off this reply and stamps it on every later request
        # (supervisor-protocol ss5). Sending only the token answers
        # wrong_incarnation.
        resp = conn.send({"cmd": "SHUTDOWN", "token": acq["token"]})
        _answer_or_exit(resp)
    except OSError as exc:
        raise typer.Exit(refuse(exc, prefix=f"supervisor {sock_path}")) from exc
    finally:
        conn.close()
