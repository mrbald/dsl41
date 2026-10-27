"""The release train's verbs (DL-407, DL-412): prepare, apply, resume, rollback and status,
run as the site's deployment identity, and the hidden service side, which they run as the
service account through sudo. The logic is release.py's and release_service.py's; this file is
the command line, and COMMANDS is what cli.py registers under release_service.GROUP."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from enum import Enum
from pathlib import Path

import typer

from dsl41 import release, release_service
from dsl41.release_service import (
    APPLY,
    CMD_RESUME,
    CMD_STATUS,
    PREPARE,
    RESUME,
    ROLLBACK,
    SERVICE,
    STATUS,
)

_DRAIN = typer.Option(
    600.0,
    "--drain-timeout",
    metavar="SECONDS",
    help="How long to wait for running jobs to finish. When it runs out, nothing is sealed,"
    " this apply's holds are released, and the verb exits 2.",
)
_HORIZON = typer.Option(
    300.0,
    "--horizon-timeout",
    metavar="SECONDS",
    help="How long to wait out the retry horizon of the last operator request, this apply's"
    " holds included. When it"
    " runs out, nothing is sealed and the verb exits 2.",
)
_OPEN = typer.Option(
    90.0,
    "--open-timeout",
    metavar="SECONDS",
    help="How long to wait for the sealed engine to exit and the new one to answer. When it"
    " runs out, the verb exits 3 and the apply stays recorded.",
)

#: the exit codes apply, resume and rollback share (DL-412)
_APPLY_CODES = f"""
Exit codes: 0 the release is active and every hold this apply took is
off; 1 the release is active and some of its holds stay on, each printed
with the command that takes it off; 2 refused: nothing was sealed, ACTIVE
is unchanged, this apply's holds are released and no apply stays recorded;
3 stopped, with the apply recorded: `{CMD_STATUS}` says what
`{CMD_RESUME}` will do; 4 the seal's outcome is unknown, with the apply
recorded: run `{CMD_RESUME}`. An interrupt or a lost session
stops it with exit 3, the apply recorded.
"""


def _with_codes(command: Callable[..., None]) -> None:
    command.__doc__ = f"{inspect.cleandoc(command.__doc__ or '')}\n\n{_APPLY_CODES.strip()}"


class StatusFormat(str, Enum):
    text = "text"
    json = "json"


def _run(action: Callable[[], int], *, records: bool = False) -> None:
    raise typer.Exit(release.main(action, records=records))


def estate_prepare(
    name: str = typer.Argument(..., help="The release's name."),
    source_dir: Path = typer.Argument(
        None,
        help="The directory that holds the release's inputs and its estate.toml.",
    ),
    requalify: bool = typer.Option(
        False,
        "--requalify",
        help="Check the installed release NAME in place, under this runtime, and take no"
        " SOURCE_DIR.",
    ),
) -> None:
    """Install a release under ESTATE_RELEASES, read-only, once it passes the checks.

    Copies SOURCE_DIR to ESTATE_RELEASES/NAME. Lints and rehearses it as the
    service account, with the launcher's ENVIRONMENT and the site's PROPERTIES,
    and holds it to the launcher's MAX_JOBS and MAX_GRACE_S. A refused release is
    removed, so its name is free again. --requalify runs the same checks on an
    installed release, after a runtime switch or for a release that was never
    prepared, and records them when they pass.

    Run it as the deployment identity.

    Exit codes: 0 prepared; 2 refused: prepare installs nothing, and
    --requalify leaves prepared.json as it was, though a release never
    prepared keeps the read-only inputs it was given; 3 interrupted.
    """
    if requalify and source_dir is not None:
        raise typer.BadParameter("--requalify takes no SOURCE_DIR.", param_hint="SOURCE_DIR")
    if not requalify and source_dir is None:
        raise typer.BadParameter(
            "the directory that holds estate.toml is required unless --requalify is given.",
            param_hint="SOURCE_DIR",
        )
    if requalify:
        _run(lambda: release.requalify(name))
    _run(lambda: release.prepare(name, source_dir))


def estate_apply(
    name: str = typer.Argument(..., help="The prepared release to apply."),
    drain_timeout: float = _DRAIN,
    horizon_timeout: float = _HORIZON,
    open_timeout: float = _OPEN,
) -> None:
    """Apply a prepared release to the running estate in one window.

    Holds each scheduled top-level job, waits for running jobs to finish, seals
    the period into NAME, names NAME in ACTIVE, starts the engine unit, checks
    the new period, and releases its own holds. It never releases a hold it
    did not apply. Run it as the deployment identity.
    """
    waits = release.Waits(drain_timeout, horizon_timeout, open_timeout)
    _run(lambda: release.apply(name, waits), records=True)


_with_codes(estate_apply)


def estate_resume(
    drain_timeout: float = _DRAIN,
    horizon_timeout: float = _HORIZON,
    open_timeout: float = _OPEN,
) -> None:
    """Finish the recorded apply from the step it reached.

    The record names the release. A seal is never sent twice: a recorded
    request id is read in the estate first. The status verb says what it
    will do.
    """
    waits = release.Waits(drain_timeout, horizon_timeout, open_timeout)
    _run(lambda: release.resume(waits), records=True)


_with_codes(estate_resume)


def estate_rollback(
    drain_timeout: float = _DRAIN,
    horizon_timeout: float = _HORIZON,
    open_timeout: float = _OPEN,
) -> None:
    """Apply the release the active one replaced, by the same window.

    The target is the release the active release's latest receipt names, so a
    second rollback goes forward again. It prints the target before it acts.
    With an apply recorded whose new period opened, it takes that apply over:
    it releases that apply's holds, writes its receipt, and rolls back to the
    release it replaced.
    """
    waits = release.Waits(drain_timeout, horizon_timeout, open_timeout)
    _run(lambda: release.rollback(waits), records=True)


_with_codes(estate_rollback)


def estate_status(
    output: StatusFormat = typer.Option(
        StatusFormat.text, "--format", metavar="FORMAT", help="text or json."
    ),
) -> None:
    """Show the active release and how the last apply ended.

    Shows the active release, whether an engine answers, where a rollback
    goes, a recorded apply with what resume will do, and how the last apply
    ended, with each hold it left on and the command that takes it off.

    Exit codes: 0 shown; 2 the launcher's configuration cannot be read.
    """
    _run(lambda: release.status(output.value))


def estate_service(
    words: list[str] = typer.Argument(None, metavar="ACTION WORD..."),
) -> None:
    """The service account's side of the release verbs, run through sudo.

    Exit codes: 0 done; 2 refused; a seal exits with the seal's own code.
    """
    _run(lambda: release_service.service(list(words or [])))


#: the verbs in `--help` order, each under its name, and whether it is hidden (DL-412)
COMMANDS = (
    (PREPARE, estate_prepare, False),
    (APPLY, estate_apply, False),
    (RESUME, estate_resume, False),
    (ROLLBACK, estate_rollback, False),
    (STATUS, estate_status, False),
    (SERVICE, estate_service, True),
)
