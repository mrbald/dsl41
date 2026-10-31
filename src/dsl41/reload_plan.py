"""`reread` as a module: load an estate from disk, diff it against the live catalog, store a plan.

This is spec-reload steps 1 to 6, without a CLI verb and without any access to the engine. The
engine's part is the `Live` record: the catalog it runs, the estate settings it runs under, the
catalog revision and each row's status. `reread` changes nothing live. It writes one directory,
`<plans_dir>/<token>/`, and a second call with the same inputs rewrites the same directory and
returns the same token.

`reread` takes the inputs `dsl41 run --manifest M [--environment E] -p SITE` takes, and loads
them with the same functions: the manifest (read without following a symlink), its files, its
properties layers (the base ones, then the environment's), the timezone map, then the site
layers `-p` adds. A manifest that declares environments needs one named, as `run` demands. JIL
and layer files are read as `run` reads them, following a symlink.

The estate is `estate.toml`, the JIL files it names in order, and `non-idempotent.toml` (absent
means empty). Every other input is a part of the candidate and of the token too, because it
changes the catalog or the settings. The parts are, in order: `estate.toml`, the files, the
manifest's layers, the timezone map, each site layer as `site/<basename>`, `non-idempotent.toml`,
then `invocation.json`: the environment name and the site layers' absolute paths `reread` was
called with, as canonical JSON, so the same files under another environment are another plan.
Each part is read once; the catalog, the settings, the token and the stored copy all come from
those bytes, and an input that changed while loading is refused. A part name may appear once,
compared case-insensitively; `estate.toml`, `non-idempotent.toml`, `invocation.json` and
`plan.json` are reserved.

The candidate settings (`estate_settings`) are the estate-wide values every job runs under:

- `timezone`: the manifest's base timezone, or `UTC` without one, the zone an engine launched
  with no `--timezone` runs under (`runner_config.runtime_profile_from_cli`);
- `timezone_aliases`: the parsed timezone map, or None without one or for an empty one, as the
  engine reads a map (`timezones.alias_table`);
- `semantics`: the manifest's semantic switches as `check_overrides` returns them, defaults
  dropped, so one profile is one value;
- `external_instances`: each `insert_xinst` record without its source span.

The engine must build `Live.settings` with the same function.

Refusals, all as `RereadRefused`: a load error, a lint finding of error or warning severity
(`--strict`), a preflight ERROR, and a running box the plan edits. The preflight is the
runner-design ss8 preflight minus the rules that need the launch profile: job type, the execution
inputs (envvars, global substitution, check files), calendars, timezones, retry, resources, oracle
construction and the skeleton cycle. Its WARNs go into `plan.json` as `preflight_warnings`.
Deferred to the engine at admission: the machine rules (`machine`, `machine-mixed`; the strict
policy and the declared identity) and the owner rule (`owner`; the invoking user). `start`
anchors the calendar probes and defaults to now, so a calendar warning can differ between two
calls on different days.

Write order in `plans/<token>/`: each candidate file by the durable-write sequence, `plan.json`
last. `plan.json` lists every part with its sha256, so `read_plan` takes a directory as complete
only when `plan.json` is present, every listed file is there with those bytes, and the token
recomputed from the parts and the live revision is the directory's name.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dsl41.ast_jil import JilFile, JilParseError, parse, render_statement
from dsl41.canon import ARTIFACT_FORMAT_VERSION, CanonError, canonical_bytes, decode
from dsl41.estate_manifest import ManifestError, read_regular, resolve_manifest
from dsl41.ir import CatalogIR, LoweringError, lower_catalog
from dsl41.lint import lint_catalog
from dsl41.placeholders import PlaceholderError, load_properties, substitute
from dsl41.reload_diff import Plan, catalog_hash_v2, diff, plan_token, running_boxes
from dsl41.runner_preflight import PreflightItem, preflight
from dsl41.runner_procid import durable_write, mkdir_durable
from dsl41.semantics import check_overrides
from dsl41.semantics import resolve as resolve_switches
from dsl41.timezones import alias_table, parse_timezone_map, resolve_timezone

#: The base zone of an estate that names none: an engine launched with no `--timezone` runs UTC.
DEFAULT_TZ = "UTC"

#: Statuses of a job with a process the plan would have to stop (spec-reload `update`).
STOPPABLE = frozenset({"RUNNING", "STARTING"})

MANIFEST = "estate.toml"
MARKED = "non-idempotent.toml"
INVOCATION = "invocation.json"
PLAN_JSON = "plan.json"
_RESERVED = frozenset({MANIFEST, MARKED, INVOCATION, PLAN_JSON})

#: Preflight codes that need the launch profile (host identity, invoking user): the engine's.
_LAUNCH_CODES = frozenset({"machine", "machine-mixed", "owner"})


class RereadRefused(Exception):
    """The estate cannot become a plan: one sentence per finding, for an exit 2."""

    def __init__(self, findings: Sequence[str]) -> None:
        super().__init__("; ".join(findings))
        self.findings = tuple(findings)


@dataclass(frozen=True)
class Live:
    """What the engine supplies: the running catalog, its settings, its revision, the statuses,
    the flags each tombstone of a removed job kept, by name (spec-reload: re-adding a removed
    name shows them), and the run numbers."""

    catalog: CatalogIR
    settings: Mapping[str, object]
    revision: int
    status: Mapping[str, str]
    tombstones: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: each row's current run number, beside its status: the run a stop interrupts
    runs: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Estate:
    """An estate as read from disk: the lowered catalog, its settings and its named bytes."""

    catalog: CatalogIR
    settings: Mapping[str, object]
    parts: tuple[tuple[str, bytes], ...]
    environment: str | None = None
    properties: tuple[str, ...] = ()
    #: the parsed JIL files, after substitution, in catalog order (`spec_texts`)
    parsed: tuple[JilFile, ...] = field(default=(), repr=False)
    #: the estate directory, absolute
    directory: str = ""


@dataclass(frozen=True)
class RereadResult:
    token: str
    plan: Plan
    stop: tuple[str, ...]
    plan_dir: Path
    plan_json: Path
    files: tuple[Path, ...]
    #: each added name a tombstone kept flags for, with the flags (`Live.tombstones`)
    restored: tuple[tuple[str, tuple[str, ...]], ...] = ()


def estate_settings(
    timezone: str | None,
    tz_aliases: Mapping[str, str] | None,
    semantics: Mapping[str, str],
    catalog: CatalogIR,
) -> dict[str, object]:
    """The estate-wide settings `reload_diff.diff` compares (module docstring). Each value is
    spelled as the engine runs it, so the engine's own settings and an unchanged estate's
    compare equal."""
    return {
        "timezone": timezone or DEFAULT_TZ,
        "timezone_aliases": alias_table(tz_aliases),
        "semantics": check_overrides(semantics),
        "external_instances": {
            name: xinst.model_dump(mode="json", exclude={"span"})
            for name, xinst in catalog.external_instances.items()
        },
    }


def _read(path: Path, *, manifest: bool = False) -> bytes:
    """PATH's bytes. The manifest is read as `parse_manifest` reads it, without following a
    symlink; any other file as `run` reads it. An absent `non-idempotent.toml` is empty."""
    if path.name == MARKED and not os.path.lexists(path):
        return b""
    try:
        return read_regular(path) if manifest else path.read_bytes()
    except ManifestError as exc:
        raise RereadRefused([str(exc)]) from exc
    except OSError as exc:
        raise RereadRefused([f"{path}: {exc.strerror}"]) from exc


def _findings(exc: Exception) -> list[str]:
    if isinstance(exc, LoweringError):
        return [finding.render() for finding in exc.findings]
    if isinstance(exc, PlaceholderError):
        return list(exc.findings)
    return [str(exc)]


def _check_names(names: Sequence[str]) -> None:
    """A part name may appear once, compared case-insensitively, and none may be reserved."""
    reserved = {name.casefold() for name in _RESERVED}
    seen: dict[str, str] = {}
    for name in names:
        key = name.casefold()
        if key in reserved:
            raise RereadRefused([f"{name!r} is a reserved name"])
        if key in seen:
            raise RereadRefused(
                [f"{name!r} and {seen[key]!r} are one name: a part may appear once"]
            )
        seen[key] = name


def load_estate(
    estate_dir: Path, properties: Sequence[Path] = (), environment: str | None = None
) -> Estate:
    """Read, substitute, parse and lower the estate in ESTATE_DIR, or raise RereadRefused.

    PROPERTIES are the site layers `run -p` adds after the manifest's. Every part is read once
    and the manifest is parsed from its bytes; the inputs are read again after loading, and one
    that changed meanwhile is refused, so the parts are the bytes the catalog came from.
    """
    directory = estate_dir.absolute()
    manifest_path = directory / MANIFEST
    manifest_bytes = _read(manifest_path, manifest=True)
    try:
        inputs = resolve_manifest(manifest_path, environment, manifest_bytes)
    except ManifestError as exc:
        raise RereadRefused([str(exc)]) from exc

    def named(joined: str) -> tuple[str, Path]:
        path = Path(joined)
        return path.relative_to(directory).as_posix(), path

    files = [named(path) for path in inputs.files]
    layers = [named(path) for path in inputs.layers]
    tz_map = [] if inputs.timezone_map is None else [named(inputs.timezone_map)]
    site = [(f"site/{path.name}", path) for path in properties]
    ordered = dict.fromkeys([*files, *layers, *tz_map, *site])
    _check_names([name for name, _ in ordered])
    parts = [(MANIFEST, manifest_bytes)]
    parts += [(name, _read(path)) for name, path in ordered]
    parts.append((MARKED, _read(directory / MARKED)))
    by_name = dict(parts)
    aliases = None
    if tz_map:
        try:
            aliases = parse_timezone_map(by_name[tz_map[0][0]].decode("utf-8"))
        except ValueError as exc:
            raise RereadRefused([f"{tz_map[0][1]}: {exc}"]) from exc
    if inputs.timezone is not None and resolve_timezone(inputs.timezone, aliases) is None:
        raise RereadRefused([f"{manifest_path}: timezone {inputs.timezone!r} is not resolvable"])
    try:
        layer_paths = [path for _, path in [*layers, *site]]
        bindings = load_properties(layer_paths) if layer_paths else None
        parsed = []
        for name, path in files:
            text = by_name[name].decode("utf-8")
            if bindings is not None:
                text, _ = substitute(text, bindings, file=str(path))
            parsed.append(parse(text, file=str(path)))
        catalog = lower_catalog(parsed, permit_unknown=False)
    except (JilParseError, LoweringError, PlaceholderError, UnicodeDecodeError, OSError) as exc:
        raise RereadRefused(_findings(exc)) from exc
    again = [(manifest_path, True), *((path, False) for _, path in ordered)]
    again.append((directory / MARKED, False))
    for (path, is_manifest), (_, data) in zip(again, parts, strict=True):
        if _read(path, manifest=is_manifest) != data:
            raise RereadRefused([f"{path}: changed while it was being read"])
    site_paths = tuple(str(path.absolute()) for path in properties)
    invocation = {"environment": environment, "properties": list(site_paths)}
    parts.append((INVOCATION, canonical_bytes(invocation)))
    return Estate(
        catalog=catalog,
        settings=estate_settings(inputs.timezone, aliases, inputs.semantics, catalog),
        parts=tuple(parts),
        environment=environment,
        properties=site_paths,
        parsed=tuple(parsed),
        directory=str(directory),
    )


def _lint_findings(estate: Estate) -> list[str]:
    """Findings that refuse under `--strict`: every error and warning; info never counts."""
    base_tz = estate.settings["timezone"]
    aliases = estate.settings["timezone_aliases"]
    assert base_tz is None or isinstance(base_tz, str)
    assert aliases is None or isinstance(aliases, dict)
    report = lint_catalog(estate.catalog, base_tz=base_tz, tz_aliases=aliases)
    return [v.render() for v in report.violations if v.severity != "info"]


def _preflight(estate: Estate, start: datetime | None) -> list[PreflightItem]:
    """The ss8 preflight without the launch-profile rules (module docstring)."""
    base_tz = estate.settings["timezone"]
    aliases = estate.settings["timezone_aliases"]
    semantics = estate.settings["semantics"]
    assert base_tz is None or isinstance(base_tz, str)
    assert aliases is None or isinstance(aliases, dict)
    assert isinstance(semantics, dict)
    items = preflight(
        estate.catalog,
        execution=True,
        start=start,
        default_tz=base_tz,
        tz_aliases=aliases,
        semantics=resolve_switches(semantics),
    )
    return [item for item in items if item.code not in _LAUNCH_CODES]


def _plan_document(
    token: str,
    live: Live,
    plan: Plan,
    stop: Sequence[str],
    estate: Estate,
    warnings: Sequence[PreflightItem],
    restored: Sequence[tuple[str, tuple[str, ...]]],
) -> dict[str, Any]:
    return {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "token": token,
        "live_rev": live.revision,
        "live_digest": catalog_hash_v2(live.catalog),
        "added": list(plan.added),
        "removed": list(plan.removed),
        "changed": [
            {
                "name": change.name,
                "old_hash": change.old_hash,
                "new_hash": change.new_hash,
                "reason": change.reason,
            }
            for change in plan.changed
        ],
        "unchanged": list(plan.unchanged),
        "stop": list(stop),
        "preflight_warnings": [
            {"code": item.code, "job": item.job, "message": item.message} for item in warnings
        ],
        "settings": dict(estate.settings),
        "estate_dir": estate.directory,
        "environment": estate.environment,
        "properties": list(estate.properties),
        "restored": [{"name": name, "flags": list(flags)} for name, flags in restored],
        "parts": [
            {"name": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in estate.parts
        ],
    }


def spec_texts(parsed: Sequence[JilFile], catalog: CatalogIR) -> dict[str, str]:
    """job -> its preserve-rendered statements, in file order (the control socket's `spec`
    verb, DL-64): every statement whose subject is a catalog job, `insert_job` and any later
    `update_job` or `delete_job`."""
    texts: dict[str, str] = {}
    for jil_file in parsed:
        for stmt in jil_file.statements:
            if stmt.subject in catalog.jobs:
                texts[stmt.subject] = texts.get(stmt.subject, "") + render_statement(stmt)
    return texts


def stop_set(plan: Plan, live: Live) -> tuple[str, ...]:
    """Changed or removed jobs with a process that is up, sorted. A box has none (spec-reload
    step 5). `reread` previews it and the update's admission finds it again."""
    names = [change.name for change in plan.changed] + list(plan.removed)
    return tuple(
        sorted(
            name
            for name in names
            if live.status.get(name) in STOPPABLE and live.catalog.jobs[name].job_type != "BOX"
        )
    )


def reread(
    estate_dir: Path,
    live: Live,
    plans_dir: Path,
    properties: Sequence[Path] = (),
    environment: str | None = None,
    *,
    start: datetime | None = None,
) -> RereadResult:
    """Spec-reload steps 1 to 6: refuse, or store the plan and return it."""
    estate = load_estate(estate_dir, properties, environment)
    findings = _lint_findings(estate)
    if findings:
        raise RereadRefused(findings)
    items = _preflight(estate, start)
    errors = [item for item in items if item.severity == "ERROR"]
    if errors:
        raise RereadRefused(
            [
                f"preflight ERROR [{item.code}]{f' {item.job}' if item.job else ''}: {item.message}"
                for item in errors
            ]
        )
    plan = diff(live.catalog, estate.catalog, live.settings, estate.settings)
    boxes = running_boxes(plan, estate.catalog, live.status)
    if boxes:
        raise RereadRefused([f"box_running {box}" for box in boxes])
    stop = stop_set(plan, live)
    restored = tuple(
        (name, tuple(live.tombstones[name])) for name in plan.added if live.tombstones.get(name)
    )
    token = plan_token(estate.parts, live.revision)
    plan_dir = plans_dir / token
    mkdir_durable(str(plan_dir))
    files: list[Path] = []
    for name, data in estate.parts:
        target = plan_dir / name
        mkdir_durable(str(target.parent))
        durable_write(str(target), data)
        files.append(target)
    plan_json = plan_dir / PLAN_JSON
    document = _plan_document(
        token, live, plan, stop, estate, [i for i in items if i.severity == "WARN"], restored
    )
    durable_write(str(plan_json), canonical_bytes(document) + b"\n")
    return RereadResult(token, plan, stop, plan_dir, plan_json, tuple(files), restored)


def read_plan(plans_dir: Path, token: str) -> dict[str, Any] | None:
    """The stored `plan.json` as a dict, or None when the directory is not a complete plan.

    Complete means `plan.json` decodes, names this token, every part it lists is a file in the
    directory with the listed sha256, and the token recomputed from those bytes and the
    recorded live revision is this token. A crash before `plan.json` leaves no plan."""
    plan_dir = plans_dir / token
    try:
        document = decode((plan_dir / PLAN_JSON).read_bytes())
        if not isinstance(document, dict) or document.get("token") != token:
            return None
        parts: list[tuple[str, bytes]] = []
        for part in document["parts"]:
            data = (plan_dir / part["name"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != part["sha256"]:
                return None
            parts.append((part["name"], data))
        if plan_token(parts, document["live_rev"]) != token:
            return None
    except (OSError, CanonError, KeyError, TypeError):
        return None
    return document


__all__ = [
    "MANIFEST",
    "INVOCATION",
    "MARKED",
    "PLAN_JSON",
    "STOPPABLE",
    "Estate",
    "Live",
    "RereadRefused",
    "RereadResult",
    "estate_settings",
    "load_estate",
    "read_plan",
    "reread",
    "spec_texts",
    "stop_set",
]
