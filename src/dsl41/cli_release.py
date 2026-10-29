"""The release train's verbs (DL-407, DL-412): prepare, requalify, apply, resume, rollback and
status, run as the site's deployment identity, and the hidden service side, which they run as
the service account through sudo. The logic is release.py's and release_service.py's; this
file is the command line, and COMMANDS is what cli.py registers under release_service.GROUP."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from enum import Enum
from pathlib import Path

import typer

from dsl41 import release
from dsl41.release import Waits
from dsl41.release_service import (
    APPLY,
    CMD_RESUME,
    CMD_STATUS,
    PREPARE,
    REQUALIFY,
    RESUME,
    ROLLBACK,
    SERVICE,
    STATUS,
    service,
)

#: each bound's default is `release.Waits`'s, the one source (DL-420)
_DRAIN = typer.Option(
    Waits.drain_s,
    "--drain-timeout",
    metavar="SECONDS",
    help="How long to wait for running jobs to finish. When it runs out, nothing is sealed,"
    " this apply's holds are released, and the verb exits 2.",
)
_HORIZON = typer.Option(
    Waits.horizon_s,
    "--horizon-timeout",
    metavar="SECONDS",
    help="How long to wait out the retry horizon of the last operator request, this apply's"
    " holds included. When it"
    " runs out, nothing is sealed and the verb exits 2.",
)
_OPEN = typer.Option(
    Waits.open_s,
    "--open-timeout",
    metavar="SECONDS",
    help="How long to wait for the sealed engine to exit and the new one to answer. When it"
    " runs out, the verb exits 3 and the apply stays recorded.",
)

#: the exit codes apply, resume and rollback share (DL-412)
_APPLY_CODES = f"""
Exit codes: 0 the release is active and every hold this apply took is
off; 1 done, and some holds this apply took stay on (the release is
active, or a refusal sealed nothing), each printed with the command that
releases it; 2 refused: nothing was sealed, ACTIVE
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


def release_prepare(
    name: str = typer.Argument(..., help="The release's name."),
    source_dir: Path = typer.Argument(
        ...,
        help="The directory that holds the release's inputs and its estate.toml.",
    ),
) -> None:
    """Install a release under ESTATE_RELEASES, read-only, once it passes the checks.

    Copies SOURCE_DIR to ESTATE_RELEASES/NAME. Lints and rehearses it as the
    service account, with the launcher's ENVIRONMENT and the site's PROPERTIES,
    and holds it to the launcher's MAX_JOBS and MAX_GRACE_S. A refused release is
    removed, so its name is free again. A release already installed is checked in
    place by `release requalify`.

    Run it as the deployment identity.

    Exit codes: 0 prepared; 2 refused, nothing installed; 3 interrupted.
    """
    _run(lambda: release.prepare(name, source_dir))


def release_requalify(
    name: str = typer.Argument(..., help="The installed release."),
) -> None:
    """Check the installed release NAME in place, under this runtime.

    Runs prepare's checks on a release already under ESTATE_RELEASES, after a
    runtime switch or for a release installed by hand, and records them when
    they pass. Apply refuses a release prepared under another dsl41 version
    until this has run.

    Run it as the deployment identity.

    Exit codes: 0 checked and recorded; 2 refused: prepared.json stays as it
    was, and a release never prepared keeps the read-only inputs it was given;
    3 interrupted.
    """
    _run(lambda: release.requalify(name))


def release_apply(
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
    waits = Waits(drain_timeout, horizon_timeout, open_timeout)
    _run(lambda: release.apply(name, waits), records=True)


_with_codes(release_apply)


def release_resume(
    drain_timeout: float = _DRAIN,
    horizon_timeout: float = _HORIZON,
    open_timeout: float = _OPEN,
) -> None:
    """Finish the recorded apply from the step it reached.

    The record names the release. A seal is never sent twice: a recorded
    request id is read in the estate first. The status verb says what it
    will do.
    """
    waits = Waits(drain_timeout, horizon_timeout, open_timeout)
    _run(lambda: release.resume(waits), records=True)


_with_codes(release_resume)


def release_rollback(
    name: str = typer.Argument(
        ..., help="The release to roll back to: the one `release status` names."
    ),
    drain_timeout: float = _DRAIN,
    horizon_timeout: float = _HORIZON,
    open_timeout: float = _OPEN,
) -> None:
    """Apply NAME, the release the active one replaced, by the same window.

    NAME must be the release `release status` names as the rollback's target: the one
    the active release's latest receipt names as its previous. Any other name is refused
    with the target printed; `release apply NAME` applies a release by name. With an apply
    recorded whose new period opened, NAME is the release that apply replaced, and the
    rollback takes that apply over: it keeps that apply's holds, writes its receipt, and
    rolls back under them; the holds are released when that rollback ends.
    """
    waits = Waits(drain_timeout, horizon_timeout, open_timeout)
    _run(lambda: release.rollback(name, waits), records=True)


_with_codes(release_rollback)


def release_status(
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


def release_service(
    words: list[str] = typer.Argument(None, metavar="ACTION WORD..."),
) -> None:
    """The service account's side of the release verbs, run through sudo.

    Exit codes: 0 done; 2 refused; a seal exits with the seal's own code.
    """
    _run(lambda: service(list(words or [])))


#: the verbs in `--help` order, each under its name, and whether it is hidden (DL-412)
COMMANDS = (
    (PREPARE, release_prepare, False),
    (REQUALIFY, release_requalify, False),
    (APPLY, release_apply, False),
    (RESUME, release_resume, False),
    (ROLLBACK, release_rollback, False),
    (STATUS, release_status, False),
    (SERVICE, release_service, True),
)
