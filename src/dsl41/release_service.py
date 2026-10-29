"""The release train's service side, and what both sides of it read (DL-407, DL-412).

`dsl41 release prepare|requalify|apply|resume|rollback|status` run as the site's deployment
identity. What reads or writes the run root, and lint and rehearse, run as the service account
through `sudo`, as `dsl41 release service ACTION WORD...`: this module's `service`. It takes release
names, a request id, a job name or prepare's source directory, never an option or a command
line. It finds the site's launcher from the runtime it runs on, never from its caller, and
reads every path and run option through `dsl41-launch vars`. A world-writable directory on
the way to the launcher passes only when root owns it and its sticky bit is set, as sudo's
secure-path rule has it (DL-412). The sudoers grant reaches `service` and the checks it calls
on its arguments, and nothing else, so together they are the door of access-model ss2 and
part of the access boundary (DL-421).

The deployment identity never writes under a release, so no path check carries the trust
(DL-412). ESTATE_RELEASES belongs to the service account, mode 0755. `prepare NAME DIR` has
this side copy DIR, entry by entry through directory descriptors, refusing links and anything
the deployment identity could not read itself, into NAME as the service account's own:
directories 0555, files 0444, and .release 0755. It lints and rehearses that copy, and
removes it on a refusal; `requalify` and the receipts are this side's writes too. The
deployment identity's apply record and lock live in .release beside ACTIVE. Every read
refuses a release with an entry anyone else could change, and the real-path and no-follow
checks on each input stay as a second line.

The service side reads the run root through the readers the engine's resume and `dsl41
journal` use (DL-139, DL-420): the lineage proof for a committed seal, the opening proof for
the period it opened, the period manifest and its bundle, and the WAL for the inputs that can
move a job's hold, with their decisions. Evidence it cannot read stops the verb; it is never
read as absent.

The apply compares two releases through `estate_manifest.manifest_catalog` and
`classify.catalog_changes`, as `estate diff` does (DL-413).
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dsl41.runner_procid import durable_write, fsync_dir

if TYPE_CHECKING:
    from dsl41.estate_manifest import EstateInputs

#: The release verbs' command group and names. Every operator line and the registration in
#: cli.py are built from these, so a rename is one edit here (DL-412).
GROUP = "release"
PREPARE, REQUALIFY, APPLY, RESUME, ROLLBACK, STATUS, SERVICE = (
    "prepare",
    "requalify",
    "apply",
    "resume",
    "rollback",
    "status",
    "service",
)
CMD_PREPARE = f"dsl41 {GROUP} {PREPARE}"
CMD_REQUALIFY = f"dsl41 {GROUP} {REQUALIFY}"
CMD_APPLY = f"dsl41 {GROUP} {APPLY}"
CMD_RESUME = f"dsl41 {GROUP} {RESUME}"
CMD_ROLLBACK = f"dsl41 {GROUP} {ROLLBACK}"
CMD_STATUS = f"dsl41 {GROUP} {STATUS}"

EXIT_OK = 0
EXIT_KEPT = 1
EXIT_REFUSED = 2
EXIT_STOPPED = 3
EXIT_UNKNOWN = 4

#: one directory name under ESTATE_RELEASES, as the launcher reads ACTIVE (DL-407)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
REQUEST_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: the launcher's SERVICE_ACCOUNT, a login name, and ENGINE_UNIT, a service unit's name
ACCOUNT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,31}")
UNIT = re.compile(r"[A-Za-z0-9:_.@-]{1,200}\.service")
#: the request kinds that can move a job's hold: the two hold verbs, FORCE_STARTJOB, which
#: clears it, and ON_NOEXEC, which supersedes it (job chart rows job.204 to job.207)
HOLD_KINDS = ("ON_HOLD", "OFF_HOLD", "FORCE_STARTJOB", "ON_NOEXEC")
#: the launcher's variables the verbs read, each once (DL-407, DL-412)
VARS = (
    "DSL41",
    "RUN_ROOT",
    "ESTATE_ANCHOR",
    "ESTATE_RELEASES",
    "ACTIVE",
    "ENVIRONMENT",
    "PROPERTIES",
    "MAX_JOBS",
    "MAX_GRACE_S",
    "NEXT_PROFILE",
    "SERVICE_ACCOUNT",
    "ENGINE_UNIT",
)


class Stop(Exception):
    """A verb stops with an exit code and its lines, one idea each."""

    def __init__(self, code: int, *lines: str) -> None:
        super().__init__(" ".join(lines))
        self.code = code
        self.lines = lines


def say(message: str) -> None:
    """One line to the operator; a session that is gone loses it and nothing else."""
    try:
        print(f"dsl41 {GROUP}: {message}", file=sys.stderr, flush=True)
    except OSError:
        pass


# ------------------------------------------------------------ configuration


@dataclass(frozen=True)
class Config:
    """The launcher's CONFIGURATION as `dsl41-launch vars` prints it."""

    launcher: Path
    dsl41: str
    run_root: Path
    estate_anchor: Path
    releases: Path
    active: Path
    environment: str
    properties: str
    max_jobs: int
    max_grace_s: float
    next_profile: list[str]
    service_account: str
    engine_unit: str

    @property
    def socket(self) -> str:
        return str(self.run_root / "control.sock")

    @property
    def state(self) -> Path:
        """The deployment identity's own state, beside ACTIVE: the apply record and its lock;
        it never writes under ESTATE_RELEASES (DL-412)."""
        return self.active.parent / ".release"

    @property
    def record(self) -> Path:
        return self.state / "apply.json"

    @property
    def lock(self) -> Path:
        return self.state / "apply.lock"


def trusted(info: os.stat_result) -> bool:
    """Owned by root or this account, and writable by no one else. A world-writable directory
    passes only when root owns it and its sticky bit is set, as /tmp: there no other account
    can rename or remove an entry it does not own (DL-412)."""
    if info.st_uid not in (0, os.geteuid()):
        return False
    if not info.st_mode & 0o022:
        return True
    return stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)


def launcher() -> tuple[Path, str]:
    """The site's launcher, `<prefix>/bin/dsl41-launch`, where `<prefix>/.venvs` holds the
    runtime this process runs on (offline-installation's layout), and its text. No argument
    and no environment variable names it. Every directory from the root down to it, and the
    file, must be a real directory or file that only root, or this account, can write; the
    text is read from the one descriptor whose identity matched that check, so what runs is
    what was checked (DL-412)."""
    runtime = Path(os.path.realpath(sys.prefix))
    if runtime.parent.name != ".venvs":
        raise Stop(
            EXIT_REFUSED,
            f"this dsl41 runs from {runtime}, which is not an installed runtime under"
            " <prefix>/.venvs.",
            "run the dsl41 that the launcher's DSL41 names.",
        )
    path = runtime.parent.parent / "bin" / "dsl41-launch"
    reinstall = "ask the site package's owner to reinstall it."
    for directory in reversed(path.parents):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or not trusted(info):
            raise Stop(
                EXIT_REFUSED,
                f"{directory} must be a real directory that only root, or the account that runs"
                f" it, can write, since it holds {path}.",
                reinstall,
            )
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise Stop(
            EXIT_REFUSED,
            f"{path} is missing.",
            "ask the site package's owner to install the launcher there.",
        ) from None
    if not stat.S_ISREG(info.st_mode) or not trusted(info):
        raise Stop(
            EXIT_REFUSED,
            f"{path} must be a plain file that only root, or the account that runs it, can write.",
            reinstall,
        )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        same = (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid)
        if same != (info.st_dev, info.st_ino, info.st_mode, info.st_uid):
            raise Stop(EXIT_REFUSED, f"{path} changed while it was checked.", reinstall)
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return path, b"".join(chunks).decode()


#: the interpreters a launcher's first line may name, and how each runs the checked text
INTERPRETERS = {
    "#!/bin/sh": ["/bin/sh", "-s"],
    "#!/bin/bash": ["/bin/bash", "-s"],
    "#!/usr/bin/env bash": ["/usr/bin/env", "bash", "-s"],
}


def config(*, releases: bool = True) -> Config:
    """The launcher's variables; a launcher that cannot give one refuses, exit 2. The checked
    text runs, never the path, under the interpreter its first line names: /bin/sh, /bin/bash
    or `/usr/bin/env bash`, and any other first line refuses (DL-412)."""
    path, text = launcher()
    interpreter = INTERPRETERS.get(text.partition("\n")[0].strip())
    if interpreter is None:
        raise Stop(
            EXIT_REFUSED,
            f"{path} starts with {text.partition(chr(10))[0]!r}; it must name /bin/sh, /bin/bash"
            " or `/usr/bin/env bash`.",
            "ask the site package's owner to fix the launcher.",
        )
    done = subprocess.run(
        [*interpreter, "vars"], input=text, capture_output=True, text=True, check=False
    )
    owner = "ask the site package's owner to fix the launcher's CONFIGURATION."
    if done.returncode != 0:
        raise Stop(
            EXIT_REFUSED,
            f"`{path} vars` exited {done.returncode}: {done.stderr.strip()}",
            owner,
        )
    values: dict[str, str] = {}
    for line in done.stdout.splitlines():
        words = shlex.split(line)
        if len(words) == 1 and "=" in words[0]:
            key, _, value = words[0].partition("=")
            values[key] = value
    missing = [key for key in VARS if key not in values]
    if missing:
        raise Stop(EXIT_REFUSED, f"`{path} vars` printed no {', '.join(missing)}.", owner)
    try:
        max_jobs, max_grace_s = int(values["MAX_JOBS"]), float(values["MAX_GRACE_S"])
    except ValueError:
        raise Stop(
            EXIT_REFUSED,
            f"`{path} vars` printed MAX_JOBS={values['MAX_JOBS']} and"
            f" MAX_GRACE_S={values['MAX_GRACE_S']}, which must be numbers.",
            owner,
        ) from None
    if not ACCOUNT.fullmatch(values["SERVICE_ACCOUNT"]) or not UNIT.fullmatch(
        values["ENGINE_UNIT"]
    ):
        raise Stop(
            EXIT_REFUSED,
            f"`{path} vars` printed SERVICE_ACCOUNT={values['SERVICE_ACCOUNT']} and"
            f" ENGINE_UNIT={values['ENGINE_UNIT']}: a login name and a .service unit.",
            owner,
        )
    # the verbs run on the runtime the launcher selects, so prepare's version and the
    # apply's are one (DL-412)
    runtime = Path(os.path.realpath(sys.prefix))
    selected = Path(os.path.realpath(values["DSL41"])).parent.parent
    if selected != runtime:
        raise Stop(
            EXIT_REFUSED,
            f"this dsl41 runs from {runtime}, and the launcher's DSL41 selects {selected}.",
            f"run `{values['DSL41']} {GROUP} ...` instead.",
        )
    if releases:
        require_releases(Path(values["ESTATE_RELEASES"]), values["SERVICE_ACCOUNT"])
    return Config(
        launcher=path,
        dsl41=values["DSL41"],
        run_root=Path(values["RUN_ROOT"]),
        estate_anchor=Path(values["ESTATE_ANCHOR"]),
        releases=Path(values["ESTATE_RELEASES"]),
        active=Path(values["ACTIVE"]),
        environment=values["ENVIRONMENT"],
        properties=values["PROPERTIES"],
        max_jobs=max_jobs,
        max_grace_s=max_grace_s,
        next_profile=shlex.split(values["NEXT_PROFILE"]),
        service_account=values["SERVICE_ACCOUNT"],
        engine_unit=values["ENGINE_UNIT"],
    )


# ----------------------------------------------------------- releases on disk


def require_releases(releases: Path, account: str) -> None:
    """ESTATE_RELEASES is a real directory of the service account's that no one else can
    write, so no other account can swap two releases while a seal reads one (DL-412)."""
    try:
        info = releases.lstat()
        owner = pwd.getpwnam(account).pw_uid
    except (OSError, KeyError) as exc:
        raise Stop(
            EXIT_REFUSED, f"{releases} or the account {account} is missing: {exc}."
        ) from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
        try:
            found = pwd.getpwuid(info.st_uid).pw_name
        except KeyError:
            found = str(info.st_uid)
        raise Stop(
            EXIT_REFUSED,
            f"{releases} belongs to {found} with mode {stat.S_IMODE(info.st_mode):04o}, not to"
            f" {account} and writable by it alone: run `chown {account} {releases}` and"
            f" `chmod 0755 {releases}` as root.",
        )


def checked_name(name: str) -> str:
    if not NAME.fullmatch(name) or name.endswith(".new"):
        raise Stop(
            EXIT_REFUSED,
            f"'{name}' is not a release name: letters, digits, '.', '_' and '-', starting with"
            " a letter or digit, and not ending in .new.",
        )
    return name


def release_dir(cfg: Config, name: str) -> Path:
    path = cfg.releases / checked_name(name)
    if path.is_symlink():
        raise Stop(
            EXIT_REFUSED,
            f"{path} is a symlink, and a release must be a real directory: the opener names the"
            " paths the seal named (DL-407).",
        )
    if not (path / "estate.toml").is_file():
        raise Stop(EXIT_REFUSED, f"{path} holds no estate.toml, so it is not a release.")
    # prepare's copy holds no symlink, so one inside a release could only point a read or a
    # seal outside it: every use of a release refuses it (DL-412)
    links = sorted(str(file.relative_to(path)) for file in path.rglob("*") if file.is_symlink())
    if links:
        raise Stop(EXIT_REFUSED, f"{name} holds symlinks: {', '.join(links)}.")
    return path


def read_active(cfg: Config) -> str:
    """ACTIVE's one line, refused as the launcher refuses it (DL-407)."""
    path = cfg.active
    if path.is_symlink() or not path.is_file():
        raise Stop(EXIT_REFUSED, f"{path} is not a plain file, so no release is active.")
    lines = path.read_text().splitlines()
    if len(lines) != 1 or not NAME.fullmatch(lines[0]):
        raise Stop(EXIT_REFUSED, f"{path} does not name one release, so the launcher refuses it.")
    return lines[0]


def runtime_version() -> str:
    return importlib.metadata.version("dsl41")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise Stop(EXIT_STOPPED, f"{path} is not a JSON object.")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Write `value` beside `path` and rename it over, both durable."""
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_text(path: Path, text: str) -> None:
    """Write `text` over `path` by the durability liturgy, mode 0644 (DL-41a, DL-151)."""
    durable_write(str(path), text.encode(), mode=0o644)


def file_digests(path: Path) -> dict[str, str]:
    """The sha256 of every file of the release, its .release directory aside, each read as a
    release input."""
    return {
        str(file.relative_to(path)): "sha256:" + hashlib.sha256(read_input(path, file)).hexdigest()
        for file in sorted(path.rglob("*"))
        if file.is_file() and ".release" not in file.relative_to(path).parts
    }


def read_input(release: Path, file: Path) -> bytes:
    """FILE's bytes, read as an input of RELEASE: its real path is inside the release, and it
    is opened without following a symlink, as a plain file. Every service-side read of a
    release goes through this, so a link the deployment identity plants reads nothing
    outside the release (DL-412)."""
    if not os.path.realpath(file).startswith(os.path.realpath(release) + os.sep):
        raise Stop(EXIT_REFUSED, f"{file} is outside {release}.")
    try:
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise Stop(EXIT_REFUSED, f"{file} cannot be read as a release input: {exc.strerror}.")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Stop(EXIT_REFUSED, f"{file} is not a plain file.")
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def release_inputs(cfg: Config, path: Path) -> EstateInputs:
    """The estate the release's manifest declares for this host, resolved once, after every
    file it names, the manifest included, is read once as a release input (DL-412, DL-420)."""
    inputs = manifest_inputs(cfg, path)
    for file in [path / "estate.toml", *(Path(name) for name in inputs.inputs())]:
        if ".release" in file.relative_to(path).parts:
            raise Stop(EXIT_REFUSED, f"{file} is in .release, which holds no release input.")
        read_input(path, file)
    return inputs


def changed_files(path: Path, prepared: dict[str, Any]) -> list[str]:
    recorded: dict[str, str] = prepared.get("files") or {}
    current = file_digests(path)
    return sorted(
        name for name in set(recorded) | set(current) if recorded.get(name) != current.get(name)
    )


def check_prepared(cfg: Config, name: str) -> None:
    """NAME is prepared under this runtime, and its files are the ones prepare checked. The
    deployment identity writes prepared.json, so a name in it outside the release refuses as
    a symlink does (DL-412)."""
    path = release_dir(cfg, name)
    prepared = read_json(path / ".release" / "prepared.json")
    root = os.path.realpath(path) + os.sep
    outside = sorted(
        str(file)
        for file in (prepared or {}).get("files") or {}
        if not os.path.realpath(path / str(file)).startswith(root)
    )
    if outside:
        raise Stop(
            EXIT_REFUSED, f"{name}'s prepared.json names files outside it: {', '.join(outside)}."
        )
    if prepared is None:
        raise Stop(
            EXIT_REFUSED,
            f"{name} is installed but not prepared.",
            f"run `{CMD_REQUALIFY} {name}` to check it in place.",
        )
    if prepared.get("dsl41_version") != runtime_version():
        raise Stop(
            EXIT_REFUSED,
            f"{name} was prepared under dsl41 {prepared.get('dsl41_version')}, and this runtime"
            f" is dsl41 {runtime_version()}.",
            f"run `{CMD_REQUALIFY} {name}` to check it again.",
        )
    moved = changed_files(path, prepared)
    if moved:
        raise Stop(
            EXIT_REFUSED,
            f"{name}'s files changed since prepare: {', '.join(moved)}.",
            "prepare the fixed inputs under a new name.",
        )
    require_sealed(cfg, name, path)


def unsealed(cfg: Config, path: Path) -> list[str]:
    """The entries of release PATH that anyone but root or the service account could change:
    each belongs to one of them, an input is writable by no one, and .release, which holds
    prepared.json and the receipts the service side writes, only by its owner (DL-412)."""
    owners = {0, pwd.getpwnam(cfg.service_account).pw_uid}
    found = []
    for entry in [path, *path.rglob("*")]:
        info = entry.lstat()
        mask = 0o022 if ".release" in entry.relative_to(path).parts else 0o222
        if info.st_uid not in owners or info.st_mode & mask:
            found.append(str(entry.relative_to(path)) if entry != path else ".")
    return sorted(found)


def require_sealed(cfg: Config, name: str, path: Path) -> None:
    found = unsealed(cfg, path)
    if found:
        raise Stop(
            EXIT_REFUSED,
            f"{name} is not sealed to the service account: {', '.join(found[:5])} can be"
            " changed by others.",
            f"run `{CMD_REQUALIFY} {name}` to seal it again.",
        )


# ------------------------------------------------------------- the service side


def service_check(cfg: Config, name: str) -> dict[str, Any]:
    """Lint and rehearse the release: what ran, its exit codes, its job count and the
    runtime's command grace. A release that holds a symlink is refused, since prepare's copy
    holds none (DL-412); rehearse runs only after lint passes, so an error prints once. Both
    read the release's manifest through `--manifest`, the site's properties after its layers
    (DL-422)."""
    from dsl41.period import CMD_GRACE_S

    path = release_dir(cfg, name)
    require_sealed(cfg, name, path)
    release_inputs(cfg, path)
    estate = manifest_option(cfg, path)
    lint = [cfg.dsl41, "lint", *estate, "-p", cfg.properties]
    linted = subprocess.run(lint, capture_output=True, text=True, check=False)
    sys.stderr.write(linted.stdout + linted.stderr)
    rehearsed: dict[str, Any] | None = None
    jobs = None
    if linted.returncode == 0:
        rehearse = [cfg.dsl41, "rehearse", *estate, "-p", cfg.properties, "--format", "json"]
        played = subprocess.run(rehearse, capture_output=True, text=True, check=False)
        sys.stderr.write(played.stderr)
        rehearsed = {"argv": rehearse, "exit": played.returncode}
        if played.returncode == 0:
            jobs = len(json.loads(played.stdout)["jobs"])
    return {
        "lint": {"argv": lint, "exit": linted.returncode},
        "rehearse": rehearsed,
        "jobs": jobs,
        "grace_s": CMD_GRACE_S,
        "runtime": os.path.realpath(cfg.dsl41),
        "dsl41_version": runtime_version(),
    }


def manifest_option(cfg: Config, path: Path) -> list[str]:
    """The release's manifest under the launcher's ENVIRONMENT, as the verbs take it (DL-422)."""
    return ["--manifest", str(path / "estate.toml"), "--environment", cfg.environment]


def manifest_inputs(cfg: Config, path: Path) -> EstateInputs:
    """The estate the release's manifest declares under the launcher's ENVIRONMENT."""
    from dsl41.estate_manifest import ManifestError, resolve_manifest

    try:
        return resolve_manifest(path / "estate.toml", cfg.environment)
    except ManifestError as exc:
        raise Stop(EXIT_REFUSED, f"{exc}.") from exc


def service_changes(cfg: Config, before: str, after: str) -> dict[str, Any]:
    """The jobs release AFTER changes or removes against release BEFORE, by the classifier's
    leaf test, a fingerprint of each resolved definition (period-model ss10.2)."""
    import typer

    from dsl41.classify import Baseline, catalog_changes
    from dsl41.estate_manifest import manifest_catalog

    catalogs = []
    for name in (before, after):
        path = release_dir(cfg, name)
        require_sealed(cfg, name, path)
        inputs = release_inputs(cfg, path)

        def read(file: Path, path: Path = path) -> bytes:
            return read_input(path, file)

        # the loader reads from a private copy of the inputs, each read as a release input, so
        # a link planted after the check reads nothing outside the release (DL-412)
        try:
            catalogs.append(manifest_catalog(inputs, [Path(cfg.properties)], read))
        except typer.Exit:
            raise Stop(EXIT_STOPPED, f"{name} does not load; the error is above.") from None
    old, new = catalogs
    jobs = catalog_changes(Baseline(catalog=old), Baseline(catalog=new))["jobs"]
    return {"changed": jobs["changed"], "removed": jobs["removed"]}


def committed(cfg: Config, request_id: str) -> dict[str, Any]:
    """Whether the seal REQUEST_ID committed, by the lineage proof the engine's resume and
    `dsl41 seal` read, `cli_common.lineage_of`: the `seal` record or the opening's link, each
    checked against the sidecar's digest (period-model ss2.2, ss11). Never ACTIVE. A lineage
    that cannot be read stops the verb: unreadable evidence is not absence (DL-420)."""
    from dsl41.cli_common import lineage_of
    from dsl41.period import seal_path

    lineage = lineage_of(cfg.run_root)
    if lineage is None:
        raise Stop(
            EXIT_STOPPED,
            f"the lineage of {cfg.run_root} cannot be read, so whether the seal under request id"
            f" {request_id} committed is unknown.",
        )
    seal = lineage.seal
    if seal is None or seal.boundary_request.request_id != request_id:
        return {"committed": False}
    return {
        "committed": True,
        "seal": str(seal_path(cfg.run_root, seal.period_id)),
        "period": seal.period_id,
        "epoch": seal.epoch,
        "actor": seal.boundary_request.claimed_actor,
        "next_period": seal.next_period.period_id,
        "next_baseline": seal.next_period.baseline_id,
        "source_bundle_hash": seal.next_period.source_bundle_hash,
    }


def service_pins(cfg: Config, request_id: str) -> dict[str, Any]:
    """What the opener wrote for the period the seal REQUEST_ID committed, read by the
    package's readers: whether its segment opens that seal (`runner_history.prove_opening`;
    the lineage proof already tied its link to the seal), its leader epochs, the bundle its
    manifest pins (`period.read_period_manifest`) and the paths that bundle was read from
    (`period.bundle_sources`). A manifest or bundle that cannot be read is named under
    `unreadable`, apart from one that is absent; a segment that cannot be read stops the verb,
    exit 3 (DL-420)."""
    from dsl41.period import bundle_sources, read_period_manifest, wal_path
    from dsl41.runner_clock import EngineError
    from dsl41.runner_history import RunHistoryError, prove_opening
    from dsl41.runner_journal import read_journal

    seal = committed(cfg, request_id)
    if not seal["committed"]:
        raise Stop(
            EXIT_STOPPED, f"no committed seal in {cfg.run_root} has request id {request_id}."
        )
    root, period = cfg.run_root, seal["next_period"]
    pins: dict[str, Any] = seal | {
        "opened": False,
        "opening_refusal": None,
        "leader_epoch": None,
        "manifest_source_bundle_hash": None,
        "sources": [],
        "unreadable": [],
    }
    segment = wal_path(root, period)
    try:
        records = read_journal(segment)
    except FileNotFoundError:
        records = []
    except (OSError, EngineError) as exc:
        raise Stop(EXIT_STOPPED, f"{segment} cannot be read: {exc}.") from exc
    if records:
        pins["opened"] = True
        try:
            prove_opening(root, records[0])
        except RunHistoryError as exc:
            pins["opening_refusal"] = str(exc)
        epochs = [int(record["epoch"]) for record in records if record.get("rec") == "leader"]
        pins["leader_epoch"] = max(epochs, default=None)
    try:
        manifest = read_period_manifest(root, period)
    except EngineError as exc:
        manifest = None
        pins["unreadable"].append(f"period {period}'s manifest cannot be read: {exc}")
    if manifest is not None:
        pins["manifest_source_bundle_hash"] = manifest.source_bundle_hash
        try:
            sources = bundle_sources(root, manifest.source_bundle_hash)
        except EngineError as exc:
            sources = []
            pins["unreadable"].append(f"period {period}'s bundle cannot be read: {exc}")
        pins["sources"] = [source.path for source in sources]
    return pins


def hold_history(cfg: Config, job: str) -> dict[str, Any]:
    """The requests that can move JOB's hold, in the run root's log order across its last
    three WAL segments, read by `runner_journal.read_journal`: each `input` of a kind in
    HOLD_KINDS that names the job, `applied` with the revision its `decision` gave the job, or
    `undecided` while no decision follows it, since recovery may still apply it (period-model
    ss2.3). An input and its decision join on `runner_history.attempt_index`. A rejected one is
    left out. A segment that cannot be read, or an input or decision without its attempt
    number, stops the verb (DL-420)."""
    from dsl41.period import wal_path, wal_segments
    from dsl41.runner_clock import EngineError
    from dsl41.runner_history import attempt_index
    from dsl41.runner_journal import read_journal

    try:
        logs = [
            (number, read_journal(wal_path(cfg.run_root, number)))
            for number in wal_segments(cfg.run_root)[-3:]
        ]
    except (OSError, EngineError) as exc:
        raise Stop(EXIT_STOPPED, f"the log of {cfg.run_root} cannot be read: {exc}.") from exc
    inputs: list[tuple[tuple[int, int | None], dict[str, Any]]] = []
    applied: dict[tuple[int, int | None], Any] = {}
    decided: set[tuple[int, int | None]] = set()
    for number, records in logs:
        for position, record in enumerate(records, 1):
            key = (number, attempt_index(record))
            if key[1] is None and record.get("rec") in ("input", "decision"):
                # an attempt number is the join's key, so a record without one is unreadable
                # evidence, never a key (DL-420)
                raise Stop(
                    EXIT_STOPPED,
                    f"{wal_path(cfg.run_root, number)} line {position}: an {record['rec']}"
                    " record without its attempt number, so the requests that moved the hold"
                    f" of {job} cannot be read.",
                )
            if record.get("rec") == "input" and (record.get("payload") or {}).get("job") == job:
                if record.get("kind") in HOLD_KINDS:
                    inputs.append((key, record))
            if record.get("rec") == "decision":
                decided.add(key)
                if record.get("decision") == "applied":
                    applied[key] = (record.get("revisions") or {}).get(f"job:{job}")
    changes = []
    for key, record in inputs:
        if key in decided and key not in applied:
            continue
        change = {"request_id": record.get("request_id"), "kind": record["kind"]}
        if key in applied:
            changes.append(change | {"decision": "applied", "rev": applied[key]})
        else:
            changes.append(change | {"decision": "undecided", "rev": None})
    return {"changes": changes}


def service_seal(cfg: Config, name: str, request_id: str) -> int:
    """The seal of the running period into release NAME, its profile restated from the
    launcher; dsl41's exit code is this command's. NAME must be prepared under this runtime
    with its files unchanged, so a hand-run seal is held to what prepare checked. The actor it
    claims is the service account's, as the engine records it for a live seal (access-model
    ss3); no caller chooses it (DL-412)."""
    check_prepared(cfg, name)
    path = release_dir(cfg, name)
    release_inputs(cfg, path)
    argv = [cfg.dsl41, "seal", "--run-root", str(cfg.run_root)]
    argv += ["--estate-anchor", str(cfg.estate_anchor), *manifest_option(cfg, path)]
    argv += ["-p", cfg.properties, *cfg.next_profile]
    argv += ["--claimed-actor", f"os/{cfg.service_account}", "--request-id", request_id]
    say(f"seal: {shlex.join(argv)}")
    return subprocess.run(argv, check=False).returncode


def engine_job_name(word: str) -> bool:
    """Whether the engine would address a job by WORD: its own envelope rule, so a name the
    engine holds is a name the service side reads the log for (DL-412)."""
    from dsl41.runner_admission import addressed_key
    from dsl41.runner_clock import EngineError

    try:
        addressed_key("ON_HOLD", {"job": word})
    except EngineError:
        return False
    return True


def caller() -> tuple[int, set[int]]:
    """The deployment identity that ran sudo, by the uid and groups sudo records, or this
    process when nothing ran it through sudo."""
    uid = int(os.environ.get("SUDO_UID") or os.geteuid())
    if "SUDO_UID" not in os.environ:
        return uid, {os.getegid(), *os.getgroups()}
    entry = pwd.getpwuid(uid)
    return uid, set(os.getgrouplist(entry.pw_name, int(os.environ.get("SUDO_GID") or entry.pw_gid)))


def readable_by(info: os.stat_result, who: tuple[int, set[int]]) -> bool:
    """Whether WHO may read the entry, and search it when it is a directory."""
    uid, groups = who
    need = 0o5 if stat.S_ISDIR(info.st_mode) else 0o4
    if info.st_uid == uid:
        bits = info.st_mode >> 6
    elif info.st_gid in groups:
        bits = info.st_mode >> 3
    else:
        bits = info.st_mode
    return bits & need == need


def open_source(source: Path, who: tuple[int, set[int]]) -> int:
    """SOURCE's directory descriptor, reached from / one component at a time without following
    a link, each directory one the caller may search: the copy reads nothing the deployment
    identity could not read itself (DL-412)."""
    if not source.is_absolute():
        raise Stop(EXIT_REFUSED, f"{source} is not an absolute path.")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    fd = os.open("/", flags)
    try:
        for part in source.parts[1:]:
            if part in ("", ".", ".."):
                raise Stop(EXIT_REFUSED, f"{source} must name its directory plainly.")
            try:
                inner = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                hint = (
                    "; the service account must be able to read every directory on its path"
                    if isinstance(exc, PermissionError)
                    else ""
                )
                raise Stop(
                    EXIT_REFUSED, f"{source} cannot be read as a release: {exc.strerror}{hint}."
                ) from None
            os.close(fd)
            fd = inner
            if not readable_by(os.fstat(fd), who):
                raise Stop(EXIT_REFUSED, f"{source} is not readable by the deployment identity.")
    except BaseException:
        os.close(fd)
        raise
    return fd


def copy_tree(fd: int, target: Path, who: tuple[int, set[int]]) -> None:
    """The tree under directory FD into TARGET, each entry opened relative to its directory's
    descriptor without following a link, and readable by the caller."""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    for entry in os.scandir(fd):
        if entry.is_symlink():
            raise Stop(EXIT_REFUSED, f"the release's inputs hold a symlink: {entry.name}.")
        directory = entry.is_dir(follow_symlinks=False)
        if not readable_by(entry.stat(follow_symlinks=False), who):
            raise Stop(EXIT_REFUSED, f"{entry.name} is not readable by the deployment identity.")
        try:
            inner = os.open(entry.name, flags | (os.O_DIRECTORY if directory else 0), dir_fd=fd)
        except OSError as exc:
            raise Stop(EXIT_REFUSED, f"{entry.name} cannot be read: {exc.strerror}.") from None
        try:
            info = os.fstat(inner)
            if not readable_by(info, who):
                raise Stop(
                    EXIT_REFUSED, f"{entry.name} is not readable by the deployment identity."
                )
            if directory:
                os.mkdir(target / entry.name, 0o755)
                copy_tree(inner, target / entry.name, who)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise Stop(EXIT_REFUSED, f"the release's inputs hold a non-file: {entry.name}.")
            with open(target / entry.name, "xb") as out:
                while chunk := os.read(inner, 65536):
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        finally:
            os.close(inner)


def install(cfg: Config, name: str, fd: int, target: Path) -> None:
    """Copy the tree under FD to TARGET as the service account's: every input directory 0555
    and every input file 0444; .release, for prepared.json and the receipts, 0755."""
    who = caller()
    os.mkdir(target, 0o755)
    try:
        copy_tree(fd, target, who)
        (target / ".release").mkdir(exist_ok=True)
        for directory, _, files in os.walk(target, topdown=False):
            here = Path(directory)
            if ".release" in here.relative_to(target).parts:
                continue
            for file in files:
                (here / file).chmod(0o444)
            here.chmod(0o555)
        fsync_dir(cfg.releases)
    except BaseException:
        remove_tree(target)
        raise


def remove_tree(path: Path) -> None:
    """A release the service side installed and refused or replaced."""
    if not path.exists():
        return
    for directory, _, _ in os.walk(path):
        Path(directory).chmod(0o755)
    shutil.rmtree(path)


def qualify(cfg: Config, name: str) -> dict[str, Any]:
    """Lint and rehearse the installed release NAME, and hold it to the launcher's ceiling;
    the check's record, or a refusal."""
    check = service_check(cfg, name)
    lines = []
    if check["lint"]["exit"] != 0:
        lines.append(f"lint exited {check['lint']['exit']}; its findings are above.")
    elif check["rehearse"]["exit"] != 0:
        lines.append(f"rehearse exited {check['rehearse']['exit']}; its error is above.")
    elif check["jobs"] > cfg.max_jobs:
        lines.append(
            f"it holds {check['jobs']} jobs, above the site's ceiling of {cfg.max_jobs} (MAX_JOBS)."
        )
    if check["grace_s"] > cfg.max_grace_s:
        lines.append(
            f"the runtime's command grace, {check['grace_s']:g} s, is above the site's"
            f" MAX_GRACE_S, {cfg.max_grace_s:g} s."
        )
        lines.append("every prepare refuses until that changes; ask the site package's owner.")
    if lines:
        raise Stop(EXIT_REFUSED, f"{name} is refused.", *lines)
    return check


def write_in_release(cfg: Config, name: str, file: str, text: str) -> None:
    """Write FILE into release NAME's .release as the service account. The release must be
    sealed, its .release the service account's own; the write goes through .release's
    directory descriptor, opened without following a link, as a temporary file renamed over
    FILE, so a swap after the check cannot redirect it (DL-412)."""
    path = release_dir(cfg, name)
    require_sealed(cfg, name, path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        top = os.open(path, flags)
    except OSError as exc:
        raise Stop(EXIT_REFUSED, f"{path} cannot be opened: {exc.strerror}.") from None
    try:
        try:
            fd = os.open(".release", flags, dir_fd=top)
        except OSError as exc:
            raise Stop(EXIT_REFUSED, f"{path}/.release cannot be opened: {exc.strerror}.") from None
    finally:
        os.close(top)
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise Stop(EXIT_REFUSED, f"{path}/.release is not the service account's.")
        durable_write(file, text.encode(), mode=0o644, dir_fd=fd)
    finally:
        os.close(fd)


def write_prepared(cfg: Config, name: str, source: str | None, check: dict[str, Any]) -> None:
    path = cfg.releases / name
    write_in_release(
        cfg,
        name,
        "prepared.json",
        json.dumps(
            {
                "name": name,
                "source": source,
                "prepared_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "prepared_by": os.environ.get("SUDO_USER") or pwd.getpwuid(os.geteuid()).pw_name,
                "environment": cfg.environment,
                "max_jobs": cfg.max_jobs,
                "max_grace_s": cfg.max_grace_s,
                "files": file_digests(path),
                **check,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    say(f"prepared {path}: {check['jobs']} jobs, under dsl41 {check['dsl41_version']}.")


def service_prepare(cfg: Config, name: str, source: str) -> None:
    """Install SOURCE as release NAME and check it, all as the service account: the
    deployment identity never writes under a release. A refused release is removed, so its
    name is free again (DL-412)."""
    target = cfg.releases / checked_name(name)
    if target.exists() or target.is_symlink():
        if (target / ".release" / "prepared.json").is_file():
            raise Stop(
                EXIT_REFUSED,
                f"{target} is prepared already, and a prepared name is installed once.",
                "prepare the inputs under a new name.",
            )
        raise Stop(
            EXIT_REFUSED,
            f"{target} exists and is not prepared.",
            f"run `{CMD_REQUALIFY} {name}` to check it in place, or prepare under a new name.",
        )
    fd = open_source(Path(source), caller())
    try:
        if ".release" in os.listdir(fd):
            raise Stop(
                EXIT_REFUSED, f"{source} holds .release: give a release's inputs, not a release."
            )
        if "estate.toml" not in os.listdir(fd):
            raise Stop(EXIT_REFUSED, f"{source} holds no estate.toml, so it is not a release.")
        install(cfg, name, fd, target)
    finally:
        os.close(fd)
    try:
        check = qualify(cfg, name)
    except BaseException as exc:
        remove_tree(target)
        if isinstance(exc, Stop):
            raise Stop(
                exc.code,
                *exc.lines,
                f"nothing is installed under {name}, so the name is free again.",
            ) from exc
        raise
    write_prepared(cfg, name, source, check)


def service_requalify(cfg: Config, name: str) -> None:
    """The checks again on installed release NAME under this runtime, its files unchanged;
    prepared.json is replaced only when they pass. A release not sealed to the service
    account is copied to a sealed one and the old tree removed, its contents unchanged
    (DL-412)."""
    path = release_dir(cfg, name)
    earlier = read_json(path / ".release" / "prepared.json")
    if earlier is not None:
        moved = changed_files(path, earlier)
        if moved:
            raise Stop(
                EXIT_REFUSED,
                f"{name}'s files changed since prepare: {', '.join(moved)}.",
                "prepare the fixed inputs under a new name.",
            )
    if unsealed(cfg, path):
        sealed, old = cfg.releases / f".{name}.sealed", cfg.releases / f".{name}.old"
        for leftover in (sealed, old):
            remove_tree(leftover)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        try:
            install(cfg, name, fd, sealed)
        finally:
            os.close(fd)
        path.rename(old)
        sealed.rename(path)
        fsync_dir(cfg.releases)
        try:
            remove_tree(old)
        except OSError as exc:
            say(f"the replaced tree stays at {old}: {exc.strerror}.")
        say(f"{name} is sealed to the service account.")
    try:
        check = qualify(cfg, name)
    except Stop as exc:
        kept = "prepared.json is unchanged." if earlier else f"{name} stays unprepared."
        raise Stop(exc.code, *exc.lines, kept) from exc
    write_prepared(cfg, name, earlier.get("source") if earlier else None, check)


#: Each receipt a finished apply leaves, by kind, with the outcome its body records. The
#: service side writes it as `KIND-ID.json`, so the kind is the file's prefix (DL-412).
RECEIPT_OUTCOMES = {
    "applied": "applied",
    "refused": "refused",
    "verify-failed": "verify_failed",
    "taken-over": "taken_over",
}
RECEIPT_KINDS = tuple(RECEIPT_OUTCOMES)


def service_receipt(cfg: Config, name: str, kind: str, receipt_id: str) -> None:
    """Write the receipt the deployment identity sends on stdin, one JSON object, as
    NAME/.release/KIND-ID.json, and name an applied one in .release/latest: nothing under a
    release is written by the deployment identity (DL-412)."""
    text = sys.stdin.read(1 << 20)
    try:
        body = json.loads(text)
    except ValueError:
        raise Stop(EXIT_REFUSED, "the receipt is not JSON.") from None
    if not isinstance(body, dict):
        raise Stop(EXIT_REFUSED, "the receipt is not a JSON object.")
    file = f"{kind}-{receipt_id}.json"
    write_in_release(cfg, name, file, json.dumps(body, indent=2, sort_keys=True) + "\n")
    if kind == "applied":
        write_in_release(cfg, name, "latest", f"{file}\n")


def service(words: list[str]) -> int:
    """`dsl41 release service ACTION WORD...`, as the service account only. Each read prints
    one JSON object; a seal exits with the seal's code; anything else refuses, exit 2."""
    # a wrong account is refused first, in its own words, then the releases directory
    cfg = config(releases=False)
    account = pwd.getpwuid(os.geteuid()).pw_name
    if account != cfg.service_account:
        raise Stop(
            EXIT_REFUSED,
            f"the service side runs as {cfg.service_account}, not as {account}.",
        )
    require_releases(cfg.releases, cfg.service_account)
    action, rest = (words[0], words[1:]) if words else ("", [])
    ids = [word for word in rest if REQUEST_ID.fullmatch(word)]
    if action == "prepare" and len(rest) == 2:
        service_prepare(cfg, rest[0], rest[1])
        return EXIT_OK
    if action == "requalify" and len(rest) == 1:
        service_requalify(cfg, rest[0])
        return EXIT_OK
    if action == "receipt" and len(rest) == 3 and rest[1] in RECEIPT_KINDS:
        if REQUEST_ID.fullmatch(rest[2]):
            service_receipt(cfg, rest[0], rest[1], rest[2])
            return EXIT_OK
    if action == "check" and len(rest) == 1:
        print(json.dumps(service_check(cfg, rest[0])))
        return EXIT_OK
    if action == "changes" and len(rest) == 2:
        print(json.dumps(service_changes(cfg, rest[0], rest[1])))
        return EXIT_OK
    if action == "seal" and len(rest) == 2 and ids == [rest[1]]:
        return service_seal(cfg, rest[0], rest[1])
    if action == "committed" and len(rest) == 1 and ids == rest:
        print(json.dumps(committed(cfg, rest[0])))
        return EXIT_OK
    if action == "holds" and len(rest) == 1 and engine_job_name(rest[0]):
        print(json.dumps(hold_history(cfg, rest[0])))
        return EXIT_OK
    if action == "pins" and len(rest) == 1 and ids == rest:
        print(json.dumps(service_pins(cfg, rest[0])))
        return EXIT_OK
    raise Stop(
        EXIT_REFUSED,
        "the service side takes prepare NAME DIR, requalify NAME, receipt NAME KIND ID, check"
        " NAME, changes FROM TO, seal NAME ID, committed ID, holds JOB or pins ID, not"
        f" {shlex.join(words) or 'nothing'}.",
    )
