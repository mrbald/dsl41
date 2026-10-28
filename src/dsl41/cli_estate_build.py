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
from typing import Any

import typer

from dsl41.cli_common import PERMIT_UNKNOWN, PROPERTIES, refuse
from dsl41.estate_manifest import ArgsFor, ManifestError, build_args


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


def _source_files(source: Path, names: list[str]) -> dict[str, bytes]:
    """The bytes of each name under SOURCE, or exit 2 on a symlink anywhere below SOURCE on
    its path, or on a name that is not a file there (DL-413)."""
    contents: dict[str, bytes] = {}
    for name in names:
        parts = Path(name).parts
        for depth in range(1, len(parts) + 1):
            if source.joinpath(*parts[:depth]).is_symlink():
                raise typer.Exit(
                    refuse(f"{source.joinpath(*parts[:depth])} is a symlink; copy the file in")
                )
        file = source / name
        if not file.is_file():
            raise typer.Exit(
                refuse(
                    f"{source}: the manifest names {name}, which is not a file there; give"
                    f" --emit {name}=MODULE when a Python module renders it"
                )
            )
        try:
            contents[name] = file.read_bytes()
        except OSError as exc:
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


def _tree_differences(target: Path, contents: dict[str, bytes]) -> list[str]:
    """One line per name that is missing from TARGET, extra in it, or holds other bytes."""
    have = {
        str(entry.relative_to(target)): entry
        for entry in target.rglob("*")
        if entry.is_symlink() or not entry.is_dir()
    }
    lines = []
    for name in sorted(contents.keys() | have.keys()):
        if name not in have:
            lines.append(f"missing  {name}")
        elif name not in contents:
            lines.append(f"extra    {name}")
        elif have[name].is_symlink() or have[name].read_bytes() != contents[name]:
            lines.append(f"differs  {name}")
    return lines


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
        " Repeatable.",
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Compare the release with the existing --out directory instead of writing.",
    ),
    permit_unknown: bool = PERMIT_UNKNOWN,
) -> None:
    """Assemble the release directory that 'dsl41 release prepare' takes.

    Each --emit renders one of the manifest's files from a Python
    module, through the loader 'dsl41 build' uses. This command imports
    each MODULE, so it executes that code: run it only on modules you
    trust. Every other file the manifest names, layers and timezone map
    included, is copied from DIRECTORY with its name, and so is
    estate.toml. An emitted name may not also be a file in DIRECTORY.
    The result's manifest is then loaded for every declared
    environment. --out is written as OUT.tmp, then renamed. --check
    writes nothing.

    Exit codes: 0 written, or equal to --out under --check; 1 --out
    differs; 2 the manifest is invalid, a file it names is missing and
    not emitted, an --emit names no manifest file or a file DIRECTORY
    holds, a copied file is a
    symlink, --out is not empty (or, under --check, not a directory),
    OUT.tmp exists, or a module did not render.
    """  # DL-413
    import shutil
    import tempfile

    from dsl41.cli_common import module_jil
    from dsl41.estate_manifest import manifest_names, parse_manifest

    source = directory.absolute()
    try:
        manifest = parse_manifest(source / "estate.toml")
        names = manifest_names(source / "estate.toml", manifest)
    except ManifestError as exc:
        raise typer.Exit(refuse(exc)) from exc
    emitted = _emits(manifest.files, emit)
    for name, module in emitted.items():
        if (source / name).exists() or (source / name).is_symlink():
            raise typer.Exit(
                refuse(f"--emit {name}={module}: {source / name} exists too; remove one of them")
            )
    contents = _source_files(source, [n for n in ["estate.toml", *names] if n not in emitted])
    target = out.absolute()
    stage = target.with_name(f"{target.name}.tmp")
    if check and not target.is_dir():
        raise typer.Exit(refuse(f"{out}: --check compares with an existing directory"))
    if not check and target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise typer.Exit(refuse(f"{out} exists and is not an empty directory"))
    if not check and (stage.exists() or stage.is_symlink()):
        raise typer.Exit(refuse(f"{stage} exists; remove it, then build again"))
    for name, module in emitted.items():
        contents[name] = module_jil(module, permit_unknown)
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


def _profile(path: Path) -> dict[str, Any]:
    """The interpretation profile the manifest at PATH declares: the base zone, the timezone
    map's digest, and every semantic switch at its effective value (period-model ss10.2)."""
    import hashlib

    from dsl41.estate_manifest import load_manifest
    from dsl41.semantics import REGISTRY

    manifest = load_manifest(path)
    tz_map = "none"
    if manifest.timezone_map is not None:
        data = (path.parent / manifest.timezone_map).read_bytes()
        tz_map = "sha256:" + hashlib.sha256(data).hexdigest()
    return {
        "timezone": manifest.timezone or "UTC",
        "timezone_map": tz_map,
        "semantics": {
            name: manifest.semantics.get(name, switch.default) for name, switch in REGISTRY.items()
        },
    }


def _diff_text(changes: dict[str, dict[str, list[str]]], profile: dict[str, Any]) -> str:
    """One section per kind that differs, one name per line, and the profile last."""
    lines: list[str] = []
    for kind, groups in changes.items():
        rows = [f"  {group:<7} {name}" for group, names in groups.items() for name in names]
        lines += [kind, *rows] if rows else []
    rows = [
        f"  {key}: {v['old']} -> {v['new']}" for key, v in profile.items() if key != "semantics"
    ]
    rows += [
        f"  semantics {name}: {v['old']} -> {v['new']}"
        for name, v in profile.get("semantics", {}).items()
    ]
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
) -> None:
    """Show what a new release changes against an old one.

    Each manifest's catalog is loaded with its own layers, then the
    site's -p layers. A job is changed when its definition fingerprint
    moves: the test the seal's classifier applies. Calendars, cycles,
    resources, machines, globals and external instances are compared
    the same way. The timezone, the timezone map's contents and each
    semantic switch's effective value are reported apart, as the
    profile.

    Exit codes: 0 no difference; 1 a difference; 2 a manifest is
    invalid, the environment is missing or not declared, or a catalog
    does not load.
    """  # DL-413, period-model ss10.2
    from dsl41.release_service import catalog_changes, manifest_catalog

    sides = []
    for path in (old.absolute(), new.absolute()):
        try:
            profile = _profile(path)
            argv = build_args(path, ArgsFor.viz, environment)
        except (ManifestError, OSError) as exc:
            raise typer.Exit(refuse(exc)) from exc
        sides.append((profile, manifest_catalog(path.parent, argv, properties or [])))
    (was, old_catalog), (now, new_catalog) = sides
    changes = catalog_changes(old_catalog, new_catalog)
    differ = {
        key: {"old": was[key], "new": now[key]}
        for key in ("timezone", "timezone_map")
        if was[key] != now[key]
    }
    switches = {
        name: {"old": value, "new": now["semantics"][name]}
        for name, value in was["semantics"].items()
        if value != now["semantics"][name]
    }
    if switches:
        differ["semantics"] = switches
    if output is DiffFormat.json:
        typer.echo(json.dumps({**changes, "profile": differ}))
    else:
        typer.echo(_diff_text(changes, differ), nl=False)
    found = differ or any(names for groups in changes.values() for names in groups.values())
    raise typer.Exit(1 if found else 0)
