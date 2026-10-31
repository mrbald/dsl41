"""The redeploy verbs `reread` and `update` (spec-reload).

Each command reads what it needs from the engine through `send`, one function: `send("catalog",
{})` answers the engine's `Live`, and `send("update", {"token", "stop_running", "wait"})`
answers an `UpdateReply`. The registered verbs bind `send` to the run root's control socket
(`socket_send`: the `catalog` read verb and the `update` mutation, docs/control-protocol.md);
a test passes its own. A command prints its result and answers its exit code; the registered
verb raises `typer.Exit` with it.

`reread` exits 0 for a plan that stops nothing, 1 for a plan that stops at least one running job,
and 2 for a refusal. `update` exits 0 when the plan is admitted, and applied with `--wait`; 1 when
`--wait` ended with the plan admitted and not yet applied; and 2 for a refusal or a token with no
complete plan. `--wait` waits on the socket as long as the engine's `status` says it waits for an
application (`update_window_s`), plus the round trip's own timeout.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uuid

import typer

from dsl41.idempotence import IdempotenceError, load_exceptions
from dsl41 import reload_plan
from dsl41.reload_plan import Live, RereadRefused, read_plan
from dsl41.reload_update import WOULD_STOP_RUNNING, Admitted, Application, Refused

#: The engine as the commands reach it: a verb and its arguments in, the reply out.
type Send = Callable[[str, Mapping[str, object]], object]

#: The plans directory of a run root.
PLANS = "plans"


@dataclass(frozen=True)
class UpdateReply:
    """The engine's answer to `update`: the admission's decision, and with `--wait` the
    application once it is done."""

    decision: Admitted | Refused
    application: Application | None = None
    #: admitted, and `--wait` ended before the application was done
    pending: bool = False


def _names(names: Sequence[str]) -> str:
    return ", ".join(names) if names else "none"


def _live(send: Send) -> Live:
    live = send("catalog", {})
    if not isinstance(live, Live):
        raise TypeError(f"the catalog verb answered {type(live).__name__}, not Live")
    return live


def reread_command(
    estate_dir: Path,
    *,
    run_root: Path,
    send: Send,
    properties: Sequence[Path] = (),
    environment: str | None = None,
) -> int:
    """Diff ESTATE_DIR against the engine's catalog and store the plan under RUN_ROOT/plans.

    Prints the added jobs with the flags a re-added name gets back, the changed and removed jobs
    with their names, the count of unchanged ones, the
    running jobs the plan stops and the marked (non-idempotent) ones among them, the preflight
    warnings, and the token. Nothing live changes.
    """
    live = _live(send)
    plans = run_root / PLANS
    try:
        result = reload_plan.reread(estate_dir, live, plans, properties, environment)
        marked = load_exceptions(result.plan_dir)
    except (RereadRefused, IdempotenceError) as exc:
        findings = exc.findings if isinstance(exc, RereadRefused) else (str(exc),)
        for finding in findings:
            typer.echo(f"reread: {finding}", err=True)
        return 2
    document: dict[str, Any] = read_plan(plans, result.token) or {}
    plan = result.plan
    typer.echo(f"plan against catalog revision {live.revision}")
    typer.echo(f"added: {_names(plan.added)}")
    for name, flags in result.restored:
        typer.echo(f"re-added with its earlier flags: {name} ({', '.join(flags)})")
    changed = [
        change.name if not change.reason else f"{change.name} ({change.reason})"
        for change in plan.changed
    ]
    typer.echo(f"changed: {_names(changed)}")
    typer.echo(f"removed: {_names(plan.removed)}")
    typer.echo(f"unchanged: {len(plan.unchanged)} jobs")
    typer.echo(f"stops running: {_names(result.stop)}")
    flagged = [job for job in result.stop if not marked.is_idempotent(job)]
    if flagged:
        typer.echo(
            "marked non-idempotent among them: "
            + ", ".join(f"{job} ({marked.reason(job)})" for job in flagged)
        )
    for item in document.get("preflight_warnings", ()):
        where = f" {item['job']}" if item["job"] else ""
        typer.echo(f"preflight WARN [{item['code']}]{where}: {item['message']}")
    typer.echo(f"token: {result.token}")
    return 1 if result.stop else 0


def _render_refusal(refusal: Refused) -> None:
    line = f"update: refused {refusal.code}"
    if refusal.names:
        line += f": {', '.join(refusal.names)}"
    if refusal.code == WOULD_STOP_RUNNING:
        line += "; --stop-running consents to stopping them"
    typer.echo(line, err=True)


def _render_application(application: Application) -> None:
    typer.echo(f"applied: catalog revision {application.rev} ({application.digest})")
    typer.echo(f"added: {_names(application.added)}")
    typer.echo(f"changed: {_names(application.changed)}")
    typer.echo(f"removed: {_names(application.removed)}")
    for name, flags in application.restored:
        typer.echo(f"re-added with its earlier flags: {name} ({', '.join(flags)})")
    for job, choice in application.stopped:
        typer.echo(f"stopped {job}: {choice}")


def update_command(
    token: str,
    *,
    run_root: Path,
    send: Send,
    stop_running: bool = False,
    wait: bool = False,
) -> int:
    """Ask the engine to apply the plan TOKEN under RUN_ROOT/plans.

    Prints the decision: admitted with the jobs it stops, or the refusal with its code and names.
    With `--wait`, the reply comes after the application, which is printed too.
    """
    if read_plan(run_root / PLANS, token) is None:
        typer.echo(f"update: no complete plan {token} under {run_root / PLANS}", err=True)
        return 2
    reply = send("update", {"token": token, "stop_running": stop_running, "wait": wait})
    if not isinstance(reply, UpdateReply):
        raise TypeError(f"the update verb answered {type(reply).__name__}, not UpdateReply")
    decision = reply.decision
    if isinstance(decision, Refused):
        _render_refusal(decision)
        return 2
    typer.echo(f"admitted: plan {decision.token}; stopping: {_names(decision.stop_set)}")
    if reply.pending:
        typer.echo(f"pending: {decision.token}")
        typer.echo("update: admitted; its stops are not done yet", err=True)
        return 1
    if reply.application is not None:
        _render_application(reply.application)
    return 0


# ---- the control socket


class SendFailed(Exception):
    """The engine could not be reached, or answered something other than the verb's answer:
    the verb exits 2 with this text."""


def socket_send(run_root: Path, *, request_id: str | None = None, timeout: float = 10.0) -> Send:
    """`send` over RUN_ROOT's control socket. `catalog` reads the engine's `Live`; `update`
    reads the read header's `baseline_id` first, sends the mutation envelope with `expect`
    naming the configuration rev the stored plan was diffed against, under `request_id` (a new
    one when None), and maps the answer to an `UpdateReply`. `timeout` bounds each round trip."""
    from dsl41.runner_control import ControlClientError, claimed_actor, roundtrip

    socket_path = run_root / "control.sock"

    def ask(request: dict[str, Any], wait_s: float = timeout) -> dict[str, Any]:
        try:
            return roundtrip(socket_path, request, timeout=wait_s)
        except ControlClientError as exc:
            raise SendFailed(str(exc)) from exc

    def send(verb: str, args: Mapping[str, object]) -> object:
        if verb == "catalog":
            return live_of_answer(ask({"cmd": "catalog"}))
        token = str(args["token"])
        plan = read_plan(run_root / PLANS, token) or {}
        # the read header names the store; the engine says how long it waits for an application
        header = ask({"cmd": "status"})
        rid = request_id or str(uuid.uuid4())
        typer.echo(f"request_id: {rid}", err=True)
        request = {
            "cmd": "update",
            "token": token,
            "stop_running": bool(args.get("stop_running")),
            "wait": bool(args.get("wait")),
            "request_id": rid,
            "baseline_id": header.get("baseline_id"),
            "expect": {"config": plan.get("live_rev")},
            "claimed_actor": claimed_actor(),
        }
        window = float(header.get("update_window_s") or 0) if args.get("wait") else 0.0
        return update_reply_of(ask(request, timeout + window))

    return send


def live_of_answer(answer: Mapping[str, Any]) -> Live:
    """The `Live` a `catalog` answer carries."""
    from dsl41.ir import CatalogIR

    if not answer.get("ok"):
        raise SendFailed(str(answer.get("error", "the engine refused the catalog read")))
    return Live(
        CatalogIR.model_validate(answer["catalog"]),
        dict(answer["settings"]),
        int(answer["revision"]),
        dict(answer["status"]),
        {name: tuple(flags) for name, flags in dict(answer.get("tombstones") or {}).items()},
        {name: int(run) for name, run in dict(answer.get("runs") or {}).items()},
    )


def update_reply_of(answer: Mapping[str, Any]) -> UpdateReply:
    """The `UpdateReply` an `update` answer carries. A refusal the update chart makes keeps its
    code and names; any other `ok: false` (no plan, a draining engine, an unknown outcome) is
    `SendFailed` with its code and text."""
    from dsl41.ir import CatalogIR

    token = str(answer.get("token", ""))
    if not answer.get("ok"):
        code = answer.get("code")
        if code in _CHART_CODES:
            return UpdateReply(Refused(token, code, tuple(answer.get("names") or ())))
        outcome = (
            ""
            if answer.get("refused")
            else " (outcome unknown: re-read, then retry under the same --request-id)"
        )
        raise SendFailed(f"update {code}: {answer.get('error')}{outcome}")
    decision = Admitted(token, tuple(answer.get("stop") or ()), frozenset())
    body = answer.get("application")
    if body is None:
        return UpdateReply(decision, pending=bool(answer.get("pending")))
    application = Application(
        token=token,
        rev=int(body["rev"]),
        digest=str(body["catalog_hash"]),
        live_digest="",
        # the answer carries the application's summary, not its catalog
        catalog=CatalogIR(),
        added=tuple(body["added"]),
        changed=tuple(body["changed"]),
        removed=tuple(body["removed"]),
        restored=tuple((name, tuple(flags)) for name, flags in body["restored"]),
        stopped=tuple((job, choice) for job, choice in body["stopped"]),
    )
    return UpdateReply(decision, application)


#: The refusals the `update` chart makes, which `update` prints as refusals.
_CHART_CODES = frozenset({"stale_plan", "box_running", "would_stop_running", "update_in_progress"})


def _run(command: Callable[[], int]) -> None:
    try:
        code = command()
    except SendFailed as exc:
        typer.echo(str(exc), err=True)
        code = 2
    raise typer.Exit(code)


_RUN_ROOT = typer.Option(..., "--run-root", help="The running engine's run root.")


def reread(
    estate_dir: Path = typer.Argument(
        ..., help="The estate directory: estate.toml, its JIL files, non-idempotent.toml."
    ),
    run_root: Path = _RUN_ROOT,
    properties: list[Path] = typer.Option(
        None,
        "--properties",
        "-p",
        help="Site properties files after the manifest's layers, as `run -p` takes them.",
    ),
    environment: str | None = typer.Option(
        None, "--environment", metavar="NAME", help="The manifest's environment."
    ),
) -> None:
    """Diff the estate on disk against the running engine's catalog.

    Prints the added, changed and removed jobs by name, the count of
    unchanged ones, the running jobs the plan would stop and the
    non-idempotent ones among them, the preflight warnings, and the
    token `dsl41 update` takes. The plan is stored under RUN_ROOT/plans;
    nothing live changes. Reads the engine through its control socket
    (the `catalog` verb, read tier).

    Exit codes: 0 a plan that stops nothing; 1 a plan that stops at
    least one running job; 2 refused (a lint or preflight error, a
    running box the plan edits, an unreachable engine).
    """
    # Design: spec-reload, docs/control-protocol.md ss4
    _run(
        lambda: reread_command(
            estate_dir,
            run_root=run_root,
            send=socket_send(run_root),
            properties=properties or (),
            environment=environment,
        )
    )


def update(
    token: str = typer.Argument(..., help="The token `dsl41 reread` printed."),
    run_root: Path = _RUN_ROOT,
    stop_running: bool = typer.Option(
        False,
        "--stop-running",
        help="Consent to stopping the running jobs the plan changes or removes"
        " (TERM, then KILL after the grace).",
    ),
    wait: bool = typer.Option(
        False, "--wait", help="Return once the update applied, and print the application."
    ),
    request_id: str | None = typer.Option(
        None,
        "--request-id",
        help="Retry an update whose outcome is unknown under the id it printed.",
    ),
) -> None:
    """Apply the plan TOKEN to the running engine.

    The engine checks the plan again against its state now. It refuses
    a plan diffed against another configuration (stale_plan), one that
    edits the membership of a running box (box_running), one that would
    stop running jobs without --stop-running (would_stop_running), and
    one sent while another update stops its jobs (update_in_progress).
    Otherwise it stops each running job the plan changes or removes,
    starts no job of the plan meanwhile, then swaps the catalog: a
    stopped job reruns under its new definition, or is held when it is
    named in non-idempotent.toml. The engine keeps running throughout.
    Needs the adm tier when an access map is armed.

    Exit codes: 0 admitted (with --wait: and applied); 1 with --wait,
    admitted and not yet applied when the engine's wait ended (the
    token is printed on a `pending:` line); 2 refused, or the engine
    could not be reached.
    """
    # Design: spec-reload, docs/control-protocol.md ss2
    _run(
        lambda: update_command(
            token,
            run_root=run_root,
            send=socket_send(run_root, request_id=request_id),
            stop_running=stop_running,
            wait=wait,
        )
    )


__all__ = [
    "PLANS",
    "Send",
    "SendFailed",
    "UpdateReply",
    "live_of_answer",
    "reread",
    "reread_command",
    "socket_send",
    "update",
    "update_command",
    "update_reply_of",
]
