"""The estate manifest, `estate.toml`: an application's one declaration of its estate inputs.

The manifest names the catalog files in command-line order, the properties layers, the base
timezone and its map, and the semantic switches. `build_args` turns it into the argument list of
one verb, and `dsl41 estate args` prints that list (DL-407). The verbs keep their own parsers
and never read the manifest.

Each name is joined to the manifest's directory as given. Nothing resolves a symlink or
normalises the path beyond pathlib's join, because `source_bundle_hash` frames each input's path
string as given (period-model ss1.1): the seal's `--next` paths and the opener's paths must be
the same strings. pathlib's form of the join is the string a verb hashes after typer turns the
argument into a Path, so the printed path and the hashed path agree.
"""

from __future__ import annotations

import re
import tomllib
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dsl41.ir import tool_version
from dsl41.semantics import check_overrides


class ManifestError(ValueError):
    """A manifest that cannot declare an estate: one sentence that names the manifest."""


class ArgsFor(str, Enum):
    """The verbs `estate args` builds for (DL-407)."""

    run = "run"
    seal = "seal"
    lint = "lint"
    rehearse = "rehearse"
    viz = "viz"


class _Strict(BaseModel):
    # an unknown key is refused at every level, and TOML types are taken strictly (DL-407)
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Environment(_Strict):
    """One `[environments.<name>]` table: its properties layers, applied after the base ones."""

    properties: list[str] = []


class Manifest(_Strict):
    """The parsed `estate.toml`; `name` appears in messages only."""

    name: str = Field(min_length=1)
    requires_dsl41: str | None = None
    files: list[str] = Field(min_length=1)
    properties: list[str] = []
    timezone: str | None = Field(default=None, min_length=1)
    timezone_map: str | None = None
    semantics: dict[str, str] = {}
    environments: dict[str, Environment] = {}


#: Longest first, so `>=` is not read as `>` and `===` not as `==`.
_OPERATORS = ("===", "==", "!=", "~=", ">=", "<=", ">", "<")
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")
_RELEASE = re.compile(r"\d+(?:\.\d+)*")


def _release(version: str) -> tuple[int, ...]:
    """The leading dotted integers of a version string; `0+unknown` gives (0,)."""
    match = _RELEASE.match(version)
    return tuple(int(part) for part in match.group().split(".")) if match else (0,)


def _check_requirement(path: Path, requirement: str, installed: str) -> None:
    """`>=X.Y[.Z]` against the installed version as a dotted-integer tuple (DL-407)."""
    text = requirement.strip()
    operator = next((op for op in _OPERATORS if text.startswith(op)), None)
    wanted = text[len(operator or "") :].strip()
    if operator is not None and operator != ">=":
        raise ManifestError(
            f"{path}: requires_dsl41 supports only '>=', not {operator!r} in {requirement!r}"
        )
    if operator is None or not _VERSION.fullmatch(wanted):
        raise ManifestError(
            f"{path}: requires_dsl41 must read '>=X.Y' or '>=X.Y.Z', not {requirement!r}"
        )
    need, have = _release(wanted), _release(installed)
    width = max(len(need), len(have))
    if have + (0,) * (width - len(have)) < need + (0,) * (width - len(need)):
        raise ManifestError(f"{path}: requires dsl41 {requirement}; this is dsl41 {installed}")


def _validation_sentence(path: Path, exc: ValidationError) -> str:
    error = exc.errors()[0]
    where = ".".join(str(part) for part in error["loc"])
    if error["type"] == "extra_forbidden":
        return f"{path}: unknown key {where}"
    if error["type"] == "missing":
        return f"{path}: missing key {where}"
    return f"{path}: {where}: {error['msg']}"


def load_manifest(path: Path) -> Manifest:
    """Parse and validate the manifest at `path`, or raise ManifestError (DL-407).

    Every name must be relative and must exist as a file under the manifest's directory, every
    environment's layers included, so a manifest that refuses for one environment refuses for
    all."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"{path}: cannot read the manifest: {exc.strerror}") from exc
    try:
        table = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        # ValueError covers TOMLDecodeError and UnicodeDecodeError; RecursionError is deep nesting
        raise ManifestError(f"{path}: not valid TOML: {exc}") from exc
    try:
        manifest = Manifest.model_validate(table)
    except ValidationError as exc:
        raise ManifestError(_validation_sentence(path, exc)) from exc
    if manifest.requires_dsl41 is not None:
        _check_requirement(path, manifest.requires_dsl41, tool_version())
    try:
        check_overrides(manifest.semantics)
    except ValueError as exc:
        raise ManifestError(f"{path}: semantics: {exc}") from exc
    names = [*manifest.files, *manifest.properties]
    names += [layer for env in manifest.environments.values() for layer in env.properties]
    if manifest.timezone_map is not None:
        names.append(manifest.timezone_map)
    for name in names:
        _join(path, name)
    return manifest


def _join(path: Path, name: str) -> str:
    """`name` joined to the manifest's directory, as the string every verb receives."""
    if not name or Path(name).is_absolute():
        raise ManifestError(f"{path}: {name!r} must be a path relative to the manifest")
    joined = path.parent / name
    if str(joined).startswith("-"):
        # pathlib drops a leading './', so the verb would read this word as an option
        raise ManifestError(f"{path}: {name!r} would reach the verb as an option")
    if not joined.is_file():
        raise ManifestError(f"{path}: {name!r} is not a file under {path.parent}")
    return str(joined)


def _each(flag: str, values: list[str]) -> list[str]:
    return [word for value in values for word in (flag, value)]


def build_args(path: Path, verb: ArgsFor, environment: str | None = None) -> list[str]:
    """The argument list `verb` takes for the estate the manifest at `path` declares (DL-407).

    run and rehearse get the positional files, `-p` per layer, `--timezone`, `--timezone-map`
    and `--semantics NAME=VALUE` each; seal gets the same as `--next FILE` each, `-p` each and
    the `--next-` options; lint stops before the semantics, and viz after the layers. The layers
    are the manifest's `properties`, then the chosen environment's."""
    manifest = load_manifest(path)
    layers = list(manifest.properties)
    if environment is not None:
        chosen = manifest.environments.get(environment)
        if chosen is None:
            declared = ", ".join(sorted(manifest.environments)) or "none"
            raise ManifestError(
                f"{path}: no environment {environment!r}; the manifest declares {declared}"
            )
        layers += chosen.properties
    files = [_join(path, name) for name in manifest.files]
    if verb is ArgsFor.seal:
        argv, prefix = _each("--next", files), "--next-"
    else:
        argv, prefix = files, "--"
    argv += _each("-p", [_join(path, layer) for layer in layers])
    if verb is ArgsFor.viz:
        return argv
    if manifest.timezone is not None:
        argv += [f"{prefix}timezone", manifest.timezone]
    if manifest.timezone_map is not None:
        argv += [f"{prefix}timezone-map", _join(path, manifest.timezone_map)]
    if verb is ArgsFor.lint:
        return argv
    pairs = [f"{name}={value}" for name, value in manifest.semantics.items()]
    return argv + _each(f"{prefix}semantics", pairs)
