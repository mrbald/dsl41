"""Socket-only preview and update clients (DL-463)."""

from __future__ import annotations

import shlex
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from dsl41.cli_control import SOCKET_OPT
from dsl41.cli_output import OutputStyle, application_lines, counted, names_text, reread_lines
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
    unchanged: Mapping[str, Any] | None = None


class SendFailed(Exception):
    """A transport failure or refused request."""


def reread_command(
    *,
    send: Send,
    style: OutputStyle = OutputStyle.focused,
    apply_command: str = "dsl41 update",
) -> int:
    answer = send("reread", {})
    if not isinstance(answer, Mapping):
        raise TypeError("reread did not answer a preview")
    if not answer.get("ok"):
        raise SendFailed(f"reread: no diff available: {answer.get('error')}")
    for line in reread_lines(answer, style, apply_command):
        typer.echo(line)
    view = answer["preview"]
    for message in [
        *view.get("lint_summary", view["lint_messages"]),
        *view["diagnostics"],
    ]:
        typer.echo(message, err=True)
    return 1 if view["stop"] else 0


def _render_refusal(refusal: Refused) -> None:
    typer.echo(
        f"update: refused {refusal.code}"
        + (f": {', '.join(refusal.names)}" if refusal.names else ""),
        err=True,
    )


def update_command(
    *,
    send: Send,
    wait: bool = False,
    style: OutputStyle = OutputStyle.focused,
    check_command: str = "dsl41 update",
) -> int:
    reply = send("update", {"wait": wait})
    if not isinstance(reply, UpdateReply):
        raise TypeError("update did not answer an UpdateReply")
    if isinstance(reply.decision, Refused):
        _render_refusal(reply.decision)
        return 2
    if reply.unchanged is not None:
        typer.echo(
            f"nothing changed: catalog revision {reply.unchanged['rev']}"
            f" ({reply.unchanged['catalog_hash']})"
        )
        return 0
    stops = reply.decision.stop_set
    if stops:
        typer.echo(f"stop requested for {counted(len(stops), 'running job')}: {names_text(stops)}")
    if reply.application is not None:
        for line in application_lines(reply.application, style):
            typer.echo(line)
    else:
        typer.echo(
            "update pending; application has not finished"
            if reply.pending
            else "update admitted; application not yet confirmed"
        )
        if reply.request_id:
            typer.echo(
                f"check with: {check_command} --request-id {shlex.quote(reply.request_id)} --wait"
            )
    return 1 if reply.pending else 0


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
            unchanged=answer.get("unchanged"),
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
    return UpdateReply(decision, application, request_id=str(answer.get("request_id", "")))


_CHART_CODES = frozenset({"stale_plan", "update_in_progress"})


def _run(command: Callable[[], int]) -> None:
    try:
        code = command()
    except SendFailed as exc:
        typer.echo(str(exc), err=True)
        code = 2
    raise typer.Exit(code)


def reread(
    socket_path: Path = SOCKET_OPT,
    style: OutputStyle = typer.Option(OutputStyle.focused, "--style", help="Text layout."),
) -> None:
    """Report current launch-config and JIL changes without applying them."""
    _run(
        lambda: reread_command(
            send=socket_send(socket_path),
            style=style,
            apply_command=f"dsl41 update -S {shlex.quote(str(socket_path))}",
        )
    )


def update(
    socket_path: Path = SOCKET_OPT,
    wait: bool = typer.Option(False, "--wait", help="Wait for application."),
    request_id: str | None = typer.Option(
        None, "--request-id", help="Look up an update whose outcome is unknown."
    ),
    style: OutputStyle = typer.Option(OutputStyle.focused, "--style", help="Text layout."),
) -> None:
    """Reread current inputs and apply, stopping and restarting affected jobs."""
    _run(
        lambda: update_command(
            send=socket_send(socket_path, request_id=request_id),
            wait=wait,
            style=style,
            check_command=f"dsl41 update -S {shlex.quote(str(socket_path))}",
        )
    )
