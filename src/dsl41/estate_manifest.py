"""The estate manifest, `estate.toml`: an application's one declaration of its estate inputs.

The manifest names the catalog files in command-line order, the properties layers, the base
timezone and its map, and the semantic switches. `resolve_manifest` reads it once into
`EstateInputs` for one environment. The verbs that read an estate take it through `--manifest`,
which `cli_common.estate_args` expands into the verb's own parameters before its one loader
(DL-422).

The manifest is given as an absolute path, and each name is joined to its directory as given.
Nothing resolves a symlink or normalises the path beyond pathlib's join, because
`source_bundle_hash` frames each input's path string as given: two spellings of one input would
hash apart. A name with a `..` part is refused, so the manifest's directory holds every input it
names. A name not in normal form (a `.` part, `//`, a `/` at either end) is refused, so one file
has one spelling, and so is a `.py` name: an estate's inputs are JIL, not Python (DL-410).
"""

from __future__ import annotations

import errno
import os
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dsl41.ir import tool_version
from dsl41.semantics import check_overrides


class ManifestError(ValueError):
    """A manifest that cannot declare an estate: one sentence that names the manifest."""


class _Strict(BaseModel):
    # an unknown key is refused at every level, and TOML types are taken strictly (DL-407)
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Environment(_Strict):
    """One `[environments.<name>]` table: its properties layers, applied after the base ones."""

    properties: list[str] = []


class Manifest(_Strict):
    """The parsed `estate.toml`; `name` is required and informative only."""

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
    """`>=X.Y[.Z]` against the installed version as a dotted-integer tuple (DL-407).

    Only the leading integers count, so a pre-release or dev build of X counts as X."""
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

    Every name must be in normal form (`parse_manifest`) and an existing file under the
    manifest's directory, every environment's layers included, so a manifest that refuses for
    one environment refuses for all. A joined path repeated among the files, or among the base
    layers and one environment's, is refused."""
    check_absolute(path)
    manifest = parse_manifest(path)
    _distinct(path, "files", manifest.files)
    for env in [Environment(), *manifest.environments.values()]:
        _distinct(path, "properties", [*manifest.properties, *env.properties])
    if manifest.timezone_map is not None:
        _join(path, manifest.timezone_map)
    return manifest


def check_absolute(path: Path) -> None:
    """Refuse a relative manifest path: two spellings of one manifest would join two path
    strings, and the bundle hash covers each path as given (DL-407, period-model ss1.1)."""
    if not path.is_absolute():
        raise ManifestError(
            f"{path}: give the manifest as an absolute path: the joined paths are hashed as given"
        )


def read_regular(path: Path) -> bytes:
    """PATH's bytes, opened without following a symlink and without blocking on a FIFO, or
    ManifestError when PATH is a symlink or not a regular file; any other failure is the
    OSError (DL-413)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP and path.is_symlink():
            raise ManifestError(f"{path}: a symlink, not a file; copy the file in") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ManifestError(f"{path}: not a regular file")
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def parse_manifest(path: Path) -> Manifest:
    """Parse and validate the manifest at `path` without looking for the files it names, or
    raise ManifestError (DL-407).

    The manifest is read without following a symlink, as a regular file. Every name must be
    in normal form: relative, with no `.` or `..` part, no `//` and no `/` at its end, so one
    file has one spelling. No name may be a `.py` file: `dsl41 build` renders a module to JIL
    (DL-410)."""
    try:
        raw = read_regular(path)
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
    inputs = [("files", manifest.files), ("properties", manifest.properties)]
    inputs += [
        (f"environments.{env}.properties", chosen.properties)
        for env, chosen in manifest.environments.items()
    ]
    if manifest.timezone_map is not None:
        inputs.append(("timezone_map", [manifest.timezone_map]))
    for key, names in inputs:
        for name in names:
            _relative(path, name)
            if name.endswith(".py"):
                raise ManifestError(
                    f"{path}: {key} names {name!r}, a Python file; a release takes no Python,"
                    f" so render it with 'dsl41 build' (DL-410)"
                )
    return manifest


def manifest_names(manifest: Manifest) -> list[str]:
    """Every name the manifest gives, once each in first-mention order: the files, the base
    layers, each environment's layers and the timezone map (DL-413). `parse_manifest` has
    refused a name not in normal form, so one file has one name here."""
    names = [*manifest.files, *manifest.properties]
    for env in manifest.environments.values():
        names += env.properties
    if manifest.timezone_map is not None:
        names.append(manifest.timezone_map)
    return list(dict.fromkeys(names))


def _distinct(path: Path, key: str, names: list[str]) -> None:
    """Join each name, and refuse a joined path the list repeats (DL-407)."""
    seen: set[str] = set()
    for name in names:
        joined = _join(path, name)
        if joined in seen:
            raise ManifestError(f"{path}: {key} names {joined} twice")
        seen.add(joined)


def _relative(path: Path, name: str) -> None:
    """Refuse a name that is not in normal form: empty or absolute, with a `.` or `..` part, a
    `//`, or a `/` at its end (DL-407, DL-413)."""
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise ManifestError(
            f"{path}: {name!r} must be a path relative to the manifest, with no '..' part,"
            " no '.' part, no '//' and no '/' at either end"
        )


def _join(path: Path, name: str) -> str:
    """`name` joined to the manifest's directory, as the string every verb receives."""
    joined = path.parent / name
    if not joined.is_file():
        raise ManifestError(f"{path}: {name!r} is not a file under {path.parent}")
    return str(joined)


@dataclass(frozen=True)
class EstateInputs:
    """The estate a manifest declares for one environment, each path the manifest's directory
    joined to its name as given (DL-407): the catalog files in command-line order, the layers
    (the base ones, then the environment's), the base timezone, the timezone map and the
    semantic switches."""

    manifest: Path
    files: tuple[str, ...]
    layers: tuple[str, ...]
    timezone: str | None
    timezone_map: str | None
    semantics: Mapping[str, str]

    def inputs(self) -> list[str]:
        """Every file the estate reads, the manifest aside: files, layers, then the map."""
        return [*self.files, *self.layers, *([self.timezone_map] if self.timezone_map else [])]


def resolve_manifest(path: Path, environment: str | None = None) -> EstateInputs:
    """The estate the manifest at `path` declares under `environment`, read once, or
    ManifestError (DL-407). A manifest that declares environments needs one chosen."""
    manifest = load_manifest(path)
    layers = list(manifest.properties)
    if environment is None and manifest.environments:
        declared = ", ".join(sorted(manifest.environments))
        raise ManifestError(f"{path}: choose an environment; the manifest declares {declared}")
    if environment is not None:
        chosen = manifest.environments.get(environment)
        if chosen is None:
            declared = ", ".join(sorted(manifest.environments)) or "none"
            raise ManifestError(
                f"{path}: no environment {environment!r}; the manifest declares {declared}"
            )
        layers += chosen.properties
    return EstateInputs(
        manifest=path,
        files=tuple(_join(path, name) for name in manifest.files),
        layers=tuple(_join(path, layer) for layer in layers),
        timezone=manifest.timezone,
        timezone_map=None if manifest.timezone_map is None else _join(path, manifest.timezone_map),
        semantics=dict(manifest.semantics),
    )
