"""Socket-only preview and update clients (DL-463)."""

from __future__ import annotations

import shlex
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from dsl41.cli_control import SOCKET_OPT
from dsl41.reload_plan import Live
from dsl41.reload_update import Admitted, Application, Refused

PLANS = "plans"
type Send = Callable[[str, Mapping[str, object]], object]


@dataclass(frozen=True)
class UpdateReply:
    decision: Admitted | Refused
    application: Application | None = None
    pending: bool = False
    request_id: str = ""


class SendFailed(Exception):
    """A transport failure or refused request."""


def _names(names: Sequence[str]) -> str:
    return ", ".join(names) if names else "none"


def reread_command(*, send: Send) -> int:
    answer = send("reread", {})
    if not isinstance(answer, Mapping):
        raise TypeError("reread did not answer a preview")
    if not answer.get("ok"):
        raise SendFailed(f"reread: no diff available: {answer.get('error')}")
    view = answer["preview"]
    typer.echo(f"source: {answer['source']['path']}")
    typer.echo(f"plan against catalog revision {answer['live_rev']}")
    typer.echo(f"added: {_names(view['added'])}")
    for item in view["restored"]:
        typer.echo(f"re-added with its earlier flags: {item['name']} ({', '.join(item['flags'])})")
    changes = [
        item["name"] if not item.get("reason") else f"{item['name']} ({item['reason']})"
        for item in view["changed"]
    ]
    typer.echo(f"changed: {_names(changes)}")
    typer.echo(f"removed: {_names(view['removed'])}")
    changed = {item["name"] for item in view["changed"]}
    for item in view["source_changes"]:
        name, before, after = item["job"], item["before"], item["after"]
        kind = (
            "added"
            if name in view["added"]
            else "removed"
            if name in view["removed"]
            else "changed"
            if name in changed
            else "moved"
        )
        origin = (
            f"{before} -> {after}"
            if before and after and before != after
            else after or before or "unknown"
        )
        typer.echo(f"source {kind} {name}: {origin}")
    if view["settings_sources"]:
        typer.echo(f"shared settings sources: {', '.join(view['settings_sources'])}")
    typer.echo(f"unchanged: {view['unchanged']} jobs")
    typer.echo(f"stops running: {_names(view['stop'])}")
    for effect in view["effects"]:
        typer.echo(f"update {effect['job']}: {effect['action']}")
    for item in view["non_idempotent"]:
        typer.echo(f"marked non-idempotent: {item['job']} ({item['reason']})")
    for message in [*view["lint_messages"], *view["diagnostics"]]:
        typer.echo(message, err=True)
    return 1 if view["stop"] else 0


def _render_refusal(refusal: Refused) -> None:
    typer.echo(
        f"update: refused {refusal.code}"
        + (f": {', '.join(refusal.names)}" if refusal.names else ""),
        err=True,
    )


def _render_application(application: Application) -> None:
    typer.echo(f"applied: catalog revision {application.rev} ({application.digest})")
    typer.echo(f"added: {_names(application.added)}")
    typer.echo(f"changed: {_names(application.changed)}")
    typer.echo(f"removed: {_names(application.removed)}")
    for name, flags in application.restored:
        typer.echo(f"re-added with its earlier flags: {name} ({', '.join(flags)})")
    for job, choice in application.stopped:
        typer.echo(f"stopped {job}: {choice}")


def update_command(*, send: Send, wait: bool = False) -> int:
    reply = send("update", {"wait": wait})
    if not isinstance(reply, UpdateReply):
        raise TypeError("update did not answer an UpdateReply")
    if isinstance(reply.decision, Refused):
        _render_refusal(reply.decision)
        return 2
    typer.echo(f"admitted: update; stopping: {_names(reply.decision.stop_set)}")
    if reply.pending:
        typer.echo(f"pending: {reply.request_id}")
        typer.echo("update: admitted; its stops are not done yet", err=True)
        return 1
    if reply.application is not None:
        _render_application(reply.application)
    return 0


def socket_send(socket_path: Path, *, request_id: str | None = None, timeout: float = 10.0) -> Send:
    from dsl41.cli_common import control_socket_error
    from dsl41.runner_control import ControlClientError, claimed_actor, roundtrip

    def ask(
        request: dict[str, Any],
        wait_s: float = timeout,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        try:
            return roundtrip(socket_path, request, timeout=wait_s, before_send=before_send)
        except ControlClientError as exc:
            raise SendFailed(control_socket_error(socket_path, exc)) from exc

    def send(verb: str, args: Mapping[str, object]) -> object:
        if verb == "reread":
            return ask({"v": 4, "cmd": "reread", "claimed_actor": claimed_actor()}, timeout + 120)
        header = ask({"cmd": "status"})
        if not header.get("ok") or not header.get("baseline_id"):
            raise SendFailed(str(header.get("error", "status has no baseline_id")))
        rid = request_id or str(uuid.uuid4())
        request = {
            "v": 4,
            "cmd": "update",
            "wait": bool(args.get("wait")),
            "retry_only": request_id is not None,
            "request_id": rid,
            "baseline_id": header["baseline_id"],
            "claimed_actor": claimed_actor(),
        }
        window = float(header.get("update_window_s") or 0) if args.get("wait") else 0
        return update_reply_of(
            ask(
                request,
                timeout + 125 + window,
                before_send=lambda: typer.echo(f"request_id: {rid}", err=True),
            )
        )

    return send


def live_of_answer(answer: Mapping[str, Any]) -> Live:
    """The `Live` a `catalog` answer carries."""
    from dsl41.ir import CatalogIR

    if not answer.get("ok"):
        raise SendFailed(str(answer.get("error", "the engine refused the catalog read")))
    if (
        answer.get("environment_view") != "ops"
        or not isinstance(answer.get("path_environment"), dict)
        or not isinstance(answer.get("settings"), dict)
        or not isinstance(answer.get("settings", {}).get("job_env"), dict)
    ):
        raise SendFailed(
            "reread requires the ops catalog view; redacted or missing environment data"
        )
    return Live(
        CatalogIR.model_validate(answer["catalog"]),
        dict(answer["settings"]),
        int(answer["revision"]),
        dict(answer["status"]),
        {name: tuple(flags) for name, flags in dict(answer.get("tombstones") or {}).items()},
        {name: int(run) for name, run in dict(answer.get("runs") or {}).items()},
        dict(answer.get("launch") or {}),
        dict(answer["path_environment"]),
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
        return UpdateReply(
            decision,
            pending=bool(answer.get("pending")),
            request_id=str(answer.get("request_id", "")),
        )
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


_CHART_CODES = frozenset({"stale_plan", "update_in_progress"})


def _run(command: Callable[[], int]) -> None:
    try:
        code = command()
    except SendFailed as exc:
        typer.echo(str(exc), err=True)
        code = 2
    raise typer.Exit(code)


def reread(socket_path: Path = SOCKET_OPT) -> None:
    """Report current launch-config and JIL changes without applying them."""

    def command() -> int:
        code = reread_command(send=socket_send(socket_path))
        typer.echo(f"next: dsl41 update -S {shlex.quote(str(socket_path))}")
        return code

    _run(command)


def update(
    socket_path: Path = SOCKET_OPT,
    wait: bool = typer.Option(False, "--wait", help="Wait for application."),
    request_id: str | None = typer.Option(
        None, "--request-id", help="Look up an update whose outcome is unknown."
    ),
) -> None:
    """Reread current inputs and apply, stopping and restarting affected jobs."""
    _run(lambda: update_command(send=socket_send(socket_path, request_id=request_id), wait=wait))
