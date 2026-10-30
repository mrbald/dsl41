"""The redeploy verbs `reread` and `update` (spec-reload), not yet registered.

Each command reads what it needs from the engine through `send`, one function: `send("catalog",
{})` answers the engine's `Live`, and `send("update", {"token", "stop_running", "wait"})`
answers an `UpdateReply`. The wiring binds `send` to the control socket (the `catalog` read
verb, the `update` mutation); a test passes its own. A command prints its result and answers
its exit code; the registered verb raises `typer.Exit` with it.

`reread` exits 0 for a plan that stops nothing, 1 for a plan that stops at least one running job,
and 2 for a refusal. `update` exits 0 when the plan is admitted, and applied with `--wait`, and 2
for a refusal or a token with no complete plan.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from dsl41.idempotence import IdempotenceError, load_exceptions
from dsl41.reload_plan import Live, RereadRefused, read_plan, reread
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
        result = reread(estate_dir, live, plans, properties, environment)
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
    if reply.application is not None:
        _render_application(reply.application)
    return 0


__all__ = ["PLANS", "Send", "UpdateReply", "reread_command", "update_command"]
