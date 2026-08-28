"""The control-plane verbs: what an operator says to a RUNNING engine
(DL-137's split).

`sendevent`, `host` and `query` speak the ss10 control protocol over the
run root's socket (docs/control-protocol.md); `ui` and `serve` attach the
ss11 TUI to it; `supervise` speaks the other protocol, the Tier-1
supervisor's (docs/supervisor-protocol.md). Every one of them is a CLIENT
-- nothing here holds engine state. Registered on the app in `cli.py`.

The four mutation exit codes (0/2/3/4) are DL-92's and are read in one
place, `cli_common.command_outcome`.
"""

from __future__ import annotations

from pathlib import Path

import typer

from dsl41.cli_common import command_outcome, import_tui_or_exit_2, read_header_of, refuse
from dsl41.runner_access import REQUIRED_TIER, Tier


# ------------------------------------------------------------ the control plane
#
# Exit codes: 0 a clean answer (an `ok` response), 2 the command never
# reached the engine (an unreachable socket, an unreadable answer, a
# refusal); `run`'s 1 -- the estate failed while running -- belongs to the
# engine and no client here can see it.
#
# `sendevent` splits 2 further, because since S3 a command can fail in three
# ways that call for three different next moves and a script that cannot tell
# them apart has to guess exactly where guessing costs most (DL-92): 2 stays
# REFUSED (nothing admitted, nothing logged -- the "never started" reading of
# 2, unchanged), 3 is REJECTED (a decision, with an index; the world moved)
# and 4 is UNKNOWN (no decision arrived; it may yet apply). 1 keeps its
# meaning and no other verb uses 3 or 4.


def _control_roundtrip(socket_path: Path, request: dict) -> dict:
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


def _answer_or_exit(response: dict) -> None:
    """Print one control answer and exit on its `ok` flag: `host list`,
    `query`'s non-brief read, and `supervise list`/`shutdown` all did this
    by hand, two of them indented and two not (DL-178h).

    Compact JSON (sort_keys=True, no indent): the split was an even two and
    two, and the tie breaks toward this file's other JSON idiom --
    `_stream_subscribe` already reads the wire as one record per line."""
    import json as json_mod

    typer.echo(json_mod.dumps(response, sort_keys=True))
    raise typer.Exit(0 if response.get("ok") else 2)


def _mutate(socket_path: Path, request: dict) -> None:
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
    help="The engine's control socket (<run_root>/control.sock).",
)


def _read_revision(socket_path: Path, key: str) -> tuple[str, int, int]:
    """The ss6 read header (`baseline_id`, `epoch`) and the current revision
    of `key` -- the read half of a read-then-write, for an operator who did
    not carry a revision in by hand.

    It narrows the race to one round trip; it does not remove it, and it
    cannot: the value of a precondition is that it names what the DECIDER
    saw, and a number this process fetched a millisecond ago is only a very
    recent guess about that. Whoever looked at a status page and then chose
    to act should pass --expect with the revision they looked at."""
    from dsl41.runner_control import read_for, revision_in

    response = _control_roundtrip(socket_path, read_for(key))
    header = read_header_of(response)
    if header is None:
        raise typer.Exit(2)
    baseline, epoch = header
    return baseline, epoch, revision_in(response, key)


def sendevent(
    event: str = typer.Argument(
        ...,
        help="STARTJOB|FORCE_STARTJOB|KILLJOB|ON_ICE|OFF_ICE|ON_HOLD|OFF_HOLD"
        "|ON_NOEXEC|OFF_NOEXEC|DISARM|SET_GLOBAL|CHANGE_STATUS"
        " (DISARM clears the job's armed latch and does nothing else)",
    ),
    socket_path: Path = _SOCKET_OPT,
    job: str = typer.Option(None, "--job", "-J", help="Target job (job verbs, CHANGE_STATUS)."),
    status: str = typer.Option(None, "--status", "-s", help="CHANGE_STATUS: the new status."),
    global_kv: str = typer.Option(None, "--global", "-G", help='SET_GLOBAL: "NAME=value".'),
    exit_code: int = typer.Option(
        None, "--exit-code", help="CHANGE_STATUS: optional exit code to record."
    ),
    expect: int = typer.Option(
        None,
        "--expect",
        help="The state_rev you read for the target (from `query status`/`global`)."
        " The command is rejected if it moved since. Omitted, this reads it first --"
        " which narrows the race to one round trip, not to nothing. 0 means"
        " 'still absent' (SET_GLOBAL's conditional create).",
    ),
    request_id: str = typer.Option(
        None,
        "--request-id",
        help="RETRY the command that carried this id, rather than issuing a new one."
        " An exact retry -- same id, same envelope -- is answered from the original"
        " decision and applies nothing twice, which is the only safe response to"
        " exit 4. A fresh uuid4 otherwise.",
    ),
) -> None:
    """Vendor-parity sendevent against a running engine (runner-design ss10),
    over the v3 protocol (concurrency-model ss6).

    Every mutation names the revision it was composed against and is
    answered with its DECISION, in four kinds that call for four different
    next moves -- so they get four exit codes rather than one failure
    (control-protocol ss3):

      0  applied.
      2  REFUSED: nothing admitted, no index consumed, and the log says
         nothing about it. Fix it and send it again; unchanged is safe too.
      3  REJECTED: a decision went against it -- the target moved between
         the read and the write. It IS in the log. Re-read and re-decide;
         resending the same envelope loses the same race.
      4  UNKNOWN: no decision arrived. NOT a failure -- the command may be
         durably admitted and about to apply. Re-read; if it must be sent
         again, send it with --request-id and the id printed on stderr."""
    from dsl41.runner_admission import addressed_key
    from dsl41.runner_clock import EngineError
    from dsl41.runner_control import claimed_actor, command

    verb = event.upper()
    payload: dict = {}
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
    baseline, epoch, current = _read_revision(socket_path, key)
    request = command(
        verb,
        payload,
        key=key,
        revision=current if expect is None else expect,
        baseline_id=baseline,
        epoch=epoch,
        request_id=request_id,
        claimed_actor=claimed_actor(),
    )
    _mutate(socket_path, request)


def release_held(
    socket_path: Path = _SOCKET_OPT,
    dry_run: bool = typer.Option(False, "--dry-run", help="List the held jobs, send nothing."),
) -> None:
    """Release every ON_HOLD job in one sweep -- the estate-wide OFF_HOLD
    the one-job `sendevent` cannot say (DL-180).

    A CLIENT-side sweep, not a protocol verb: ONE `status` read both
    selects the held jobs and supplies the revision every OFF_HOLD envelope
    is composed against -- the sweep acts on exactly what it saw, so a job
    that moved in between is REJECTED with its own printed decision rather
    than re-released over a state nobody looked at. A release that loses
    the race to another operator applies as the journaled no-op
    (control-protocol ss3, DL-158's "the OFF_HOLD shape"); applying one
    also re-evaluates the job's start (SEM-21), exactly as the one-job
    verb does.

    Exit codes: 2 when the status read failed (nothing was sent);
    otherwise 0 when every release applied, 1 when any per-job decision
    did not -- the per-job answers, each prefixed `-- <job>`, carry
    `sendevent`'s 0/2/3/4 detail, which one aggregate number cannot."""
    from dsl41.runner_admission import addressed_key
    from dsl41.runner_control import claimed_actor, command

    response = _control_roundtrip(socket_path, {"cmd": "status"})
    if not response.get("ok"):
        raise typer.Exit(refuse(str(response.get("error", "status query failed"))))
    header = read_header_of(response)
    if header is None:
        raise typer.Exit(2)
    baseline, epoch = header
    rows = response.get("jobs", {})
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
        request = command(
            "OFF_HOLD",
            payload,
            key=addressed_key("OFF_HOLD", payload),
            revision=int(rows[name]["state_rev"]),
            baseline_id=baseline,
            epoch=epoch,
            request_id=None,
            claimed_actor=claimed_actor(),
        )
        typer.echo(f"-- {name}")
        if command_outcome(socket_path, request) != 0:
            all_applied = False
    raise typer.Exit(0 if all_applied else 1)


def host(
    action: str = typer.Argument(..., help="list|drain|activate|evict"),
    host_id: str = typer.Argument(None, help="The host id (all but `list`)."),
    socket_path: Path = _SOCKET_OPT,
    force: bool = typer.Option(
        False,
        "--force",
        help="evict: skip the ss8 preconditions. Recorded with the actor that"
        " claimed it, and the one path in the concurrency model that can produce"
        " a double run -- use it with out-of-band proof the machine is dead.",
    ),
    expect: int = typer.Option(
        None,
        "--expect",
        help="The state_rev you read for the host (from `host list`). The command"
        " is rejected if it moved since. Omitted, this reads it first.",
    ),
    request_id: str = typer.Option(
        None, "--request-id", help="RETRY the command that carried this id (see `sendevent`)."
    ),
) -> None:
    """The ss8 routing table: which execution hosts take new work
    (concurrency-model ss8).

      list      the table, with each host's revision.
      drain     stop routing NEW work here; running work finishes. Reversible,
                asserts nothing, and is the tool for planned maintenance.
      activate  route here again, and re-dispatch what the drain held.
      evict     declare this host's work rerouteable. The only state that lets
                another host run what was bound to this one, so it is refused
                unless the leader has recorded the host unreachable, the host
                runs a deadman, and the kill bound has passed.

    Mutations take `sendevent`'s four exit codes (0 applied / 2 refused /
    3 rejected / 4 unknown) for the same reason: four outcomes, four next
    moves."""
    from dsl41.oracle_state import RuntimeState
    from dsl41.runner_control import claimed_actor, command

    verb = action.lower()
    if verb == "list":
        _answer_or_exit(_control_roundtrip(socket_path, {"cmd": "hosts"}))
    if not host_id:
        raise typer.Exit(refuse(f"`host {verb}` needs a host id"))
    key = RuntimeState.host_key(host_id)
    baseline, epoch, current = _read_revision(socket_path, key)
    request = command(
        verb,
        {"id": host_id, "force": force},
        key=key,
        revision=current if expect is None else expect,
        baseline_id=baseline,
        epoch=epoch,
        request_id=request_id,
        claimed_actor=claimed_actor(),
        cmd="host",
    )
    _mutate(socket_path, request)


def ui(socket_path: Path = _SOCKET_OPT) -> None:
    """Attach the ss11 Textual TUI to a running engine: jobs table, explain
    pane with per-atom truth, log tail, sendevent console. A thin client of
    the control socket only -- quitting detaches the viewer and leaves the
    run alone (unlike `run --ui`, whose terminal owns the run)."""
    runner_tui = import_tui_or_exit_2()
    if not socket_path.exists():
        raise typer.Exit(refuse(f"control socket {socket_path}: no such file"))
    runner_tui.RunnerApp(socket_path).run()


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
        help="Bind address (loopback default: textual-serve ships no"
        " auth, ss11 -- put a proxy or tunnel in front for remote access).",
    ),
    port: int = typer.Option(8000, "--port", help="Bind port."),
) -> None:
    """Serve the ss11 TUI over the web via textual-serve: one app subprocess
    per browser session, each `dsl41 ui --socket` against this same running
    engine -- never in-process with the engine, so no viewer gets a private
    universe (ss11). No auth of its own; see README's deployment notes
    before exposing this beyond loopback."""
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


def _stream_subscribe(socket_path: Path, request: dict) -> None:
    """`query subscribe`: print the ack, then journal records, until the
    engine hangs up or the operator interrupts.

    Presentation only (DL-172): `runner_control.subscribe_lines` owns the
    protocol -- the stamped version, the bounded read, and the DL-151 rule
    that a refusal ack does NOT close the connection. Its two exception
    types keep the CLI's exit-code mapping distinguishable the way
    `_control_roundtrip` already reads `ControlClientError` (DL-78):
    `ControlClientError` is the engine's own refusal text, printed as-is;
    `OSError` is a transport failure, prefixed with the socket like every
    other client here."""
    from dsl41.runner_control import ControlClientError, subscribe_lines

    try:
        for line in subscribe_lines(socket_path, request):
            typer.echo(line)
    except ControlClientError as exc:
        raise typer.Exit(refuse(exc)) from exc
    except OSError as exc:
        raise typer.Exit(refuse(exc, prefix=f"control socket {socket_path}")) from exc
    except KeyboardInterrupt:
        pass


def query(
    what: str = typer.Argument(
        ...,
        help="|".join([*_QUERY_VERBS, *_QUERY_PREDICATES]),
    ),
    socket_path: Path = _SOCKET_OPT,
    job: str = typer.Option(
        None, "--job", "-J", help="status: filter; explain/spec/deps/is-*: the job."
    ),
    name: list[str] = typer.Option(
        None, "--name", "-N", help="global/globals: the global(s) to read. Repeatable."
    ),
    since: int = typer.Option(None, "--since", help="trace/subscribe: only records after SEQ."),
    brief: bool = typer.Option(
        False,
        "--brief",
        help="status: one line per job (name, status, at, run, exit, flags, rev)"
        " instead of the JSON document -- the estate-scale skim (DL-66).",
    ),
) -> None:
    """Read-only control-plane queries (runner-design ss10); `subscribe`
    streams journal records as JSON lines until interrupted. The headless
    autorep analog -- the ss11 TUI consumes the same verbs. `is-success` /
    `is-failed` are scriptable predicates (DL-65): print the current status
    and exit 0 when it matches (SUCCESS; FAILURE or TERMINATED), 1 when it
    does not -- shell glue's `systemctl is-active` analog.

    `status` and `global`/`globals` are the reads a `sendevent --expect` is
    composed from (concurrency-model ss6): both publish the `state_rev` of a
    NAMED entity, and `global` answers an unset name at revision 0 rather
    than omitting it, because absence you cannot name is absence you cannot
    lock against."""
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
    request: dict = {"cmd": verb}
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
    action: str = typer.Argument(..., help="list|shutdown"),
    run_root: Path = typer.Option(
        ..., "--run-root", help="Run directory holding supervisor.sock (ss6a Tier 1)."
    ),
) -> None:
    """Observe or stop a run-root's supervisor (runner-design ss6a; DL-42 item
    4 -- read-only by default). `list` prints its live runs and lease; `shutdown`
    ACQUIREs the lease (failing loudly with holder info while an engine holds an
    unexpired one), then SHUTDOWNs: TERM->grace->KILL each command, wrappers
    record truthfully, socket + pidfile removed. Exit 2 when there is no
    supervisor or the lease could not be taken; 0 on a clean shutdown."""
    import json as json_mod
    import os

    from dsl41.runner_adapters import SupervisorConn

    verb = action.lower()
    if verb not in ("list", "shutdown"):
        raise typer.Exit(refuse(f"unknown supervise action {action!r} (list|shutdown)"))
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
