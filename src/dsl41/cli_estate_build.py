"""`dsl41 estate build` and `dsl41 estate diff`: the release an author hands over (DL-413).

`estate build` assembles a release directory from a manifest, its files and the Python modules
that render some of them, through `cli_common.module_jil`. `estate diff` compares two releases
through `release_service.manifest_catalog` and `catalog_changes`, as the seal's classifier
would. `cli.py` registers both in the `estate` sub-app.
"""

from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from dsl41.cli_common import PERMIT_UNKNOWN, PROPERTIES, load_tz_aliases, refuse
from dsl41.estate_manifest import ArgsFor, ManifestError, build_args

if TYPE_CHECKING:
    from dsl41.classify import Baseline
    from dsl41.ir import CatalogIR


def _emits(files: list[str], emit: list[str]) -> dict[str, Path]:
    """Each `--emit NAME=MODULE` as NAME to MODULE, or exit 2: NAME must be one of the
    manifest's files, and given once (DL-413)."""
    emitted: dict[str, Path] = {}
    for item in emit:
        name, sep, module = item.partition("=")
        if not sep or not name or not module:
            raise typer.Exit(refuse(f"--emit {item!r}: give NAME=MODULE, such as jobs.jil=jobs.py"))
        if name not in files:
            raise typer.Exit(refuse(f"--emit {name}: the manifest's files do not name it"))
        if name in emitted:
            raise typer.Exit(refuse(f"--emit {name}: given twice"))
        emitted[name] = Path(module)
    return emitted


def _rendered(emitted: dict[str, Path], permit_unknown: bool) -> dict[str, bytes]:
    """Each emitted NAME's JIL, or exit 2. A module that composes files renders its file NAME
    and is imported once for all the names it renders (DL-413, DL-418)."""
    from dsl41.cli_common import ModuleJil, module_jil

    rendered: dict[Path, ModuleJil] = {}
    contents: dict[str, bytes] = {}
    for name, module in emitted.items():
        key = module.resolve()  # two spellings of one path import it once
        if key not in rendered:
            rendered[key] = module_jil(module, permit_unknown)
        if not rendered[key].files:
            contents[name] = rendered[key].whole
        elif name in rendered[key].files:
            contents[name] = rendered[key].files[name]
        else:
            listed = ", ".join(rendered[key].files)
            raise typer.Exit(refuse(f"--emit {name}={module}: {module} composes {listed}"))
    return contents


def _source_files(source: Path, names: list[str]) -> dict[str, bytes]:
    """The bytes of each name under SOURCE, each opened without following a symlink, or exit 2
    on a symlink anywhere below SOURCE on its path, or on a name that is not a regular file
    there (DL-413)."""
    from dsl41.estate_manifest import read_regular

    contents: dict[str, bytes] = {}
    for name in names:
        parts = Path(name).parts
        for depth in range(1, len(parts)):
            if source.joinpath(*parts[:depth]).is_symlink():
                raise typer.Exit(
                    refuse(f"{source.joinpath(*parts[:depth])}: a symlink; copy the files in")
                )
        file = source / name
        if not file.exists() and not file.is_symlink():
            raise typer.Exit(
                refuse(
                    f"{source}: the manifest names {name}, which is not a file there; give"
                    f" --emit {name}=MODULE when a Python module renders it"
                )
            )
        try:
            contents[name] = read_regular(file)
        except (ManifestError, OSError) as exc:
            raise typer.Exit(refuse(exc)) from exc
    return contents


def _assemble(stage: Path, contents: dict[str, bytes], environments: list[str]) -> None:
    """Write CONTENTS under the empty directory STAGE, then load its manifest for every
    declared environment, or for none when it declares none (DL-413)."""
    for name, data in contents.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    chosen: list[str | None] = [*environments] or [None]
    for environment in chosen:
        build_args(stage / "estate.toml", ArgsFor.run, environment)


def _entries(root: Path) -> dict[str, os.DirEntry[str]]:
    """Every entry under ROOT that is not a directory, symlinks included, by its name relative
    to ROOT. A directory that cannot be listed raises its OSError, so no entry is missed
    unreported (DL-413)."""
    found: dict[str, os.DirEntry[str]] = {}
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as listing:
            for entry in listing:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                else:
                    found[str(Path(entry.path).relative_to(root))] = entry
    return found


def _tree_differences(target: Path, contents: dict[str, bytes]) -> list[str]:
    """One line per name that is missing from TARGET, extra in it, or holds other bytes. An
    entry that is not a regular file differs."""
    have = _entries(target)
    lines = []
    for name in sorted(contents.keys() | have.keys()):
        if name not in have:
            lines.append(f"missing  {name}")
        elif name not in contents:
            lines.append(f"extra    {name}")
        elif (
            not have[name].is_file(follow_symlinks=False)
            or Path(have[name].path).read_bytes() != contents[name]
        ):
            lines.append(f"differs  {name}")
    return lines


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def estate_build(
    directory: Path = typer.Argument(
        ..., help="The source directory: estate.toml and every file it names."
    ),
    out: Path = typer.Option(
        ...,
        "--out",
        "-o",
        help="The release directory to write. It must not exist, or be empty.",
    ),
    emit: list[str] = typer.Option(
        [],
        "--emit",
        metavar="NAME=MODULE",
        help="Render the manifest's file NAME from the Python MODULE, as 'dsl41 build' does."
        " When MODULE composes files, as a decompiled package does, NAME is one of them."
        " Repeatable.",
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Compare the release with the existing --out directory instead of writing.",
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
    param: list[str] = typer.Option([], "--param", hidden=True),  # DL-418
) -> None:
    """Assemble the release directory that 'dsl41 release prepare' takes.

    Each --emit renders one of the manifest's files from a Python
    module, through the loader 'dsl41 build' uses. A module that
    composes files, such as a decompiled package, renders the file of
    that name, and is imported once for every --emit that names it.
    This command imports each MODULE, so it executes that code: run it
    only on modules you trust. Placeholders stay as tokens: the
    manifest's layers bind them. Every other file the manifest names, layers and timezone map
    included, is copied from DIRECTORY with its name, and so is
    estate.toml. An emitted name may not also be a file in DIRECTORY,
    and the manifest names no .py file, so no Python enters the
    release. The result's manifest is then loaded for every declared
    environment. --out is written as OUT.tmp, then renamed. --check
    writes nothing.

    Exit codes: 0 written, or equal to --out under --check; 1 --out
    differs; 2 the manifest is invalid, a file it names is missing and
    not emitted, an --emit names no manifest file or a file DIRECTORY
    holds, a copied file is a symlink or not a regular file, --out is
    not empty (or, under --check, not a directory), an entry under
    --out cannot be read, OUT.tmp exists, a module did not render or
    composes no file of its NAME, or --param is given.
    """  # DL-413
    import shutil
    import tempfile

    from dsl41.estate_manifest import manifest_names, parse_manifest

    if param:
        raise typer.Exit(
            refuse(
                "--param binds placeholders for 'dsl41 build' alone; a release binds them"
                " through the manifest's layers"
            )
        )
    source = directory.absolute()
    try:
        manifest = parse_manifest(source / "estate.toml")
    except ManifestError as exc:
        raise typer.Exit(refuse(exc)) from exc
    emitted = _emits(manifest.files, emit)
    for name, module in emitted.items():
        if (source / name).exists() or (source / name).is_symlink():
            raise typer.Exit(
                refuse(f"--emit {name}={module}: {source / name} exists too; remove one of them")
            )
    names = ["estate.toml", *manifest_names(manifest)]
    contents = _source_files(source, [name for name in names if name not in emitted])
    target = out.absolute()
    stage = target.with_name(f"{target.name}.tmp")
    try:
        if check and not target.is_dir():
            raise typer.Exit(refuse(f"{out}: --check compares with an existing directory"))
        if not check and target.exists() and (not target.is_dir() or os.listdir(target)):
            raise typer.Exit(refuse(f"{out} exists and is not an empty directory"))
    except OSError as exc:
        raise typer.Exit(refuse(exc)) from exc
    if not check and (stage.exists() or stage.is_symlink()):
        raise typer.Exit(refuse(f"{stage} exists; remove it, then build again"))
    contents |= _rendered(emitted, permit_unknown)
    environments = sorted(manifest.environments)
    try:
        if check:
            with tempfile.TemporaryDirectory(prefix="dsl41-estate-") as scratch:
                _assemble(Path(scratch), contents, environments)
            differences = _tree_differences(target, contents)
        else:
            stage.mkdir()
            try:
                _assemble(stage, contents, environments)
                os.rename(stage, target)
            except BaseException:
                shutil.rmtree(stage, ignore_errors=True)
                raise
            _fsync_dir(target.parent)
    except (ManifestError, OSError) as exc:
        raise typer.Exit(refuse(exc)) from exc
    if not check:
        typer.echo(f"wrote {out}")
        return
    if not differences:
        typer.echo(f"{out} matches {directory}")
        return
    typer.echo("\n".join(differences))
    raise typer.Exit(1)


class DiffFormat(str, Enum):
    """`estate diff --format`: one section per kind, or one JSON object (DL-413)."""

    text = "text"
    json = "json"


def _baseline(path: Path, catalog: CatalogIR) -> Baseline:
    """CATALOG with the interpretation profile the manifest at PATH declares: its base zone,
    the alias table its timezone map parses to, and its switches. The launcher's half takes
    its defaults on both sides, so only the manifest can move the profile (DL-407, DL-413)."""
    from dsl41.classify import Baseline
    from dsl41.estate_manifest import load_manifest
    from dsl41.period import runtime_profile_from_cli
    from dsl41.semantics import check_overrides

    manifest = load_manifest(path)
    tz_map = None if manifest.timezone_map is None else path.parent / manifest.timezone_map
    profile = runtime_profile_from_cli(
        timezone=manifest.timezone,
        tz_aliases=load_tz_aliases(tz_map),
        semantics=check_overrides(manifest.semantics),
    )
    return Baseline(catalog=catalog, profile=profile)


def _diff_text(changes: dict[str, Any]) -> str:
    """One section per kind that differs, one name per line, each changed calendar's and
    resource's referring jobs under it, and the profile last."""
    lines: list[str] = []
    for kind, groups in changes.items():
        if kind == "profile":
            continue
        rows = []
        for group in ("added", "removed", "changed"):
            for name in groups[group]:
                rows.append(f"  {group:<7} {name}")
                if group == "changed":
                    referrers = groups.get("referenced_by", {}).get(name, [])
                    rows += [f"    referenced by {job}" for job in referrers]
        lines += [kind, *rows] if rows else []
    profile = changes["profile"]
    rows = []
    if "timezone" in profile:
        rows.append(f"  timezone: {profile['timezone']['old']} -> {profile['timezone']['new']}")
    for key in ("timezone_map", "semantics"):
        for name, values in profile.get(key, {}).items():
            rows.append(f"  {key} {name}: {values['old'] or 'none'} -> {values['new'] or 'none'}")
    lines += ["profile", *rows] if rows else []
    return "".join(f"{line}\n" for line in lines) or "no difference\n"


def estate_diff(
    old: Path = typer.Argument(..., help="The older release's estate.toml."),
    new: Path = typer.Argument(..., help="The newer release's estate.toml."),
    environment: str = typer.Option(
        None,
        "--environment",
        help="The environment whose layers follow each manifest's own. Required when the"
        " manifests declare environments.",
    ),
    properties: list[Path] = PROPERTIES,
    output: DiffFormat = typer.Option(
        DiffFormat.text,
        "--format",
        metavar="FORMAT",
        help="text: one section per kind, one name per line. json: one object.",
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
) -> None:
    """Show what a new release changes against an old one.

    Each manifest's catalog is loaded with its own layers, then the
    site's -p layers. Every job, box, calendar, cycle, resource,
    machine, global and external instance is compared by the seal's
    own definition test: a job by its fingerprint, a machine by the
    fields resolution reads, a calendar by its date set. Each changed
    calendar and resource lists the jobs that name it. The base zone,
    each timezone alias and each semantic switch are reported apart,
    as the profile. What the seal adds is not here: the jobs a change
    reaches through other records, and whether each is running, armed
    or latent.

    Exit codes: 0 no difference; 1 a difference; 2 a manifest is
    invalid, the environment is missing or not declared, the timezone
    map does not parse, or a catalog does not load.
    """  # DL-413, period-model ss10.2
    from dsl41.release_service import catalog_changes, manifest_catalog

    sides = []
    for path in (old.absolute(), new.absolute()):
        try:
            argv = build_args(path, ArgsFor.viz, environment)
        except ManifestError as exc:
            raise typer.Exit(refuse(exc)) from exc
        catalog = manifest_catalog(
            path.parent, argv, properties or [], permit_unknown=permit_unknown
        )
        sides.append(_baseline(path, catalog))
    changes = catalog_changes(*sides)
    if output is DiffFormat.json:
        typer.echo(json.dumps(changes))
    else:
        typer.echo(_diff_text(changes), nl=False)
    found = changes["profile"] or any(
        groups[group]
        for kind, groups in changes.items()
        if kind != "profile"
        for group in ("added", "removed", "changed")
    )
    raise typer.Exit(1 if found else 0)
