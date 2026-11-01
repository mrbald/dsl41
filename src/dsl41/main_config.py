"""Resolve a host config into captured inputs for the one compiler (DL-437)."""

from __future__ import annotations

import base64
import glob
import hashlib
import os
import stat
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

import typer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dsl41.ast_jil import JilParseError, parse
from dsl41.canon import canonical_bytes, decode
from dsl41.estate_manifest import ManifestError, check_requirement, read_regular
from dsl41.idempotence import Exceptions, IdempotenceError, parse_exceptions
from dsl41.ir import LoweringError, lower_catalog, tool_version
from dsl41.lint import RULE_CODES, LintReport, lint_catalog, rule_l025
from dsl41.placeholders import PlaceholderError, properties_from_bytes, substitute
from dsl41.reload_plan import Estate, RereadRefused, estate_settings
from dsl41.runner_config import RuntimeProfile, runtime_profile_from_cli
from dsl41.semantics import check_overrides
from dsl41.timezones import parse_timezone_map, resolve_timezone

DEFAULT_CONFIG = Path("/etc/dsl41/dsl41.toml")
CAPTURE = "main-inputs.json"
CONFIG_OPT = typer.Option(
    None, "--config", help="Absolute main config path; default /etc/dsl41/dsl41.toml."
)


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Environment(StrictModel):
    properties: list[str] = []


class HostConfig(StrictModel):
    run_root: str = Field(min_length=1)
    as_machine: list[str] = []
    machine_policy: Literal["strict", "local-eligible"] = "strict"
    access_map: str | None = None


class EngineConfig(HostConfig):
    environment: str | None = None


class EstateConfig(StrictModel):
    include: list[str] = Field(min_length=1)
    properties: list[str] = []
    timezone: str = Field(default="UTC", min_length=1)
    timezone_map: str | None = None
    semantics: dict[str, str] = {}
    non_idempotent: str | None = None
    environments: dict[str, Environment] = {}


class LintPolicy(StrictModel):
    refuse: Literal["error", "warn", "none"] = "none"
    ignore: list[str] = []

    def blocks(self, severity: str) -> bool:
        return self.refuse != "none" and (
            severity == "error" or (severity == "warn" and self.refuse == "warn")
        )

    def describe(self) -> str:
        return f"lint policy: refuse={self.refuse}; ignore={','.join(self.ignore) or 'none'}"


class MainConfig(StrictModel):
    format_version: int
    requires_dsl41: str | None = None
    engine: EngineConfig
    estate: EstateConfig
    lint: LintPolicy = LintPolicy()


def joined(config: Path, value: str, *, pattern: bool = False) -> str:
    """Keep declared paths, with relative names anchored at the config (DL-437)."""
    if value == "/":
        return value
    components = value.removeprefix("/").split("/")
    if any(part in ("", ".", "..") for part in components) or "\x00" in value:
        raise ValueError(
            f"{config}: invalid path {value!r}: empty, '.', '..' or repeated separator"
        )
    if "**" in value or "{" in value or "}" in value:
        raise ValueError(f"{config}: unsupported path expansion {value!r}")
    if not pattern and glob.has_magic(value):
        raise ValueError(f"{config}: globs are not allowed here: {value!r}")
    return str(Path(value) if value.startswith("/") else config.parent / value)


def load_config(path: Path, raw: bytes | None = None) -> MainConfig:
    """Validate the selected config without opening estate inputs (DL-437)."""
    try:
        config = MainConfig.model_validate(read_document(path, raw))
        if config.format_version != 1:
            raise ValueError(f"unsupported format_version {config.format_version!r}; expected 1")
        if config.requires_dsl41 is not None:
            check_requirement(path, config.requires_dsl41, tool_version())
        engine, estate = config.engine, config.estate
        if not Path(engine.run_root).is_absolute():
            raise ValueError("engine.run_root must be absolute")
        paths = [(engine.run_root, False)]
        paths += [(value, True) for value in [*estate.include, *estate.properties]]
        for name, env in estate.environments.items():
            if not name:
                raise ValueError("estate.environments names must be nonempty")
            paths += [(value, True) for value in env.properties]
        paths += [
            (value, False)
            for value in (engine.access_map, estate.timezone_map, estate.non_idempotent)
            if value is not None
        ]
        for value, pattern in paths:
            joined(path, value, pattern=pattern)
        if any(not name.strip() for name in engine.as_machine):
            raise ValueError("engine.as_machine names must be nonempty")
        if estate.environments and engine.environment is None:
            raise ValueError("engine.environment must select a declared environment")
        if engine.environment is not None and engine.environment not in estate.environments:
            raise ValueError(f"unknown engine.environment {engine.environment!r}")
        check_overrides(estate.semantics)
        unknown = set(config.lint.ignore) - RULE_CODES
        if unknown:
            raise ValueError(f"lint.ignore: unknown codes {', '.join(sorted(unknown))}")
        if estate.timezone_map is None and resolve_timezone(estate.timezone) is None:
            raise ValueError(f"estate.timezone {estate.timezone!r} is not resolvable")
        return config
    except (ValueError, OSError, ManifestError, ValidationError, RecursionError) as exc:
        raise RereadRefused([f"{path}: {exc}"]) from exc


def read_document(path: Path, raw: bytes | None = None) -> dict[str, Any]:
    """Read the selected regular TOML file without interpreting estate settings (DL-437)."""
    try:
        if not path.is_absolute():
            raise ValueError("--config must be an absolute path")
        joined(path, str(path))
        return tomllib.loads((read_regular(path) if raw is None else raw).decode("utf-8"))
    except (ValueError, OSError, ManifestError, RecursionError) as exc:
        raise RereadRefused([f"{path}: {exc}"]) from exc


def load_run_root(path: Path) -> Path:
    """Update reads only the engine location; the stored plan owns interpretation (DL-437)."""
    document = read_document(path)
    try:
        value = document["engine"]["run_root"]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("engine.run_root must be a nonempty absolute path")
        joined(path, value)
        return Path(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise RereadRefused([f"{path}: engine.run_root: {exc}"]) from exc


def load_host(path: Path) -> HostConfig:
    """Restart validates format and current host settings; environment stays pending (DL-437)."""
    document = read_document(path)
    try:
        version = document.get("format_version")
        if type(version) is not int or version != 1:
            raise ValueError(f"unsupported format_version {version!r}; expected 1")
        engine = document.get("engine")
        if not isinstance(engine, dict):
            raise ValueError("engine must be a table")
        names = ("run_root", "as_machine", "machine_policy", "access_map")
        host = HostConfig.model_validate({key: engine[key] for key in names if key in engine})
        if not Path(host.run_root).is_absolute():
            raise ValueError("engine.run_root must be absolute")
        joined(path, host.run_root)
        if host.access_map is not None:
            joined(path, host.access_map)
        if any(not name.strip() for name in host.as_machine):
            raise ValueError("engine.as_machine names must be nonempty")
        return host
    except (ValueError, ValidationError) as exc:
        raise RereadRefused([f"{path}: engine: {exc}"]) from exc


def launch_settings(config: MainConfig | HostConfig, path: Path) -> dict[str, object]:
    host = config.engine if isinstance(config, MainConfig) else config
    return {
        "run_root": host.run_root,
        "as_machine": sorted(set(host.as_machine)),
        "machine_policy": host.machine_policy,
        "access_map": None if host.access_map is None else joined(path, host.access_map),
    }


def expand(path: Path, patterns: Sequence[str], diagnostics: list[str]) -> list[str]:
    """Expand in pattern order and refuse lexical overlap (DL-437)."""
    result: list[str] = []
    seen: dict[str, str] = {}
    for pattern in patterns:
        absolute = joined(path, pattern, pattern=True)
        matches = sorted(glob.glob(absolute)) if glob.has_magic(absolute) else [absolute]
        if not matches:
            diagnostics.append(f"{path}: unmatched pattern {pattern!r}")
        for match in matches:
            if match in seen:
                raise RereadRefused([f"{match}: selected twice by {seen[match]!r} and {pattern!r}"])
            seen[match] = pattern
            result.append(match)
    return result


def input_bytes(path: str) -> bytes:
    """Follow input symlinks, but never block on or skip a special file (DL-437)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("not a regular file")
            chunks = []
            while chunk := os.read(fd, 65536):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    except (ValueError, OSError) as exc:
        raise RereadRefused([f"{path}: {exc}"]) from exc


def prepare(path: Path) -> Estate:
    """Capture, render Python, compile once and recheck files and membership (DL-437)."""
    from dsl41.cli_common import module_builder

    diagnostics: list[str] = []
    try:
        raw = read_regular(path)
        config = load_config(path, raw)
        sources = sorted(expand(path, config.estate.include, diagnostics))
        if not sources:
            raise RereadRefused([*diagnostics, f"{path}: no catalog files selected"])
        patterns = list(config.estate.properties)
        if config.engine.environment is not None:
            patterns += config.estate.environments[config.engine.environment].properties
        properties = expand(path, patterns, diagnostics)
        timezone_map = (
            None if config.estate.timezone_map is None else joined(path, config.estate.timezone_map)
        )
        marked = joined(path, config.estate.non_idempotent or "non-idempotent.toml")
        optional = config.estate.non_idempotent is None
        records: list[dict[str, str]] = []
        parts: list[tuple[str, bytes]] = []

        def add(original: str, role: str, data: bytes) -> str:
            name = f"parts/{len(parts):06d}"
            records.append({"name": name, "path": original, "role": role})
            parts.append((name, data))
            return name

        add(str(path), "config", raw)
        for source in sources:
            add(source, "python" if source.endswith(".py") else "jil", input_bytes(source))
        for prop in properties:
            add(prop, "properties", input_bytes(prop))
        if timezone_map is not None:
            add(timezone_map, "timezone_map", input_bytes(timezone_map))
        absent = optional and not os.path.lexists(marked)
        add(marked, "exceptions", b"" if absent else input_bytes(marked))
        captured = list(zip(records, parts, strict=True))
        for record, (_, data) in captured:
            if record["role"] == "python":
                module = Path(record["path"])
                try:
                    data.decode("utf-8")
                    generated = module_builder(module, source=data).to_jil().encode("utf-8")
                except (Exception, SystemExit) as exc:
                    raise RereadRefused([f"{module}: Python preparation failed: {exc}"]) from exc
                add(str(module), "generated", generated)
        metadata: dict[str, Any] = {
            "format_version": 1,
            "config_path": str(path),
            "config": config.model_dump(mode="json"),
            "launch": launch_settings(config, path),
            "records": records,
            "diagnostics": diagnostics,
            "absent_exceptions": absent,
        }
        parts.append((CAPTURE, canonical_bytes(metadata)))
        estate = from_parts(parts)
        for record, (_, data) in captured:
            original = record["path"]
            if record["role"] == "exceptions" and absent:
                changed = os.path.lexists(original)
            else:
                again = (
                    read_regular(Path(original))
                    if record["role"] == "config"
                    else input_bytes(original)
                )
                changed = again != data
            if changed:
                raise RereadRefused([f"{original}: changed during preparation"])
        if (
            sorted(expand(path, config.estate.include, [])) != sources
            or expand(path, patterns, []) != properties
        ):
            raise RereadRefused([f"{path}: glob membership changed during preparation"])
        return estate
    except RereadRefused as exc:
        if diagnostics and not all(item in exc.findings for item in diagnostics):
            raise RereadRefused([*diagnostics, *exc.findings]) from exc
        raise
    except (ValueError, OSError, ManifestError) as exc:
        raise RereadRefused([f"{path}: {exc}"]) from exc


def from_parts(parts: Sequence[tuple[str, bytes]]) -> Estate:
    """Compile captured JIL, never import Python or open an original path (DL-437)."""
    try:
        values = dict(parts)
        metadata: Any = decode(values[CAPTURE])
        if not isinstance(metadata, dict):
            raise ValueError("main-inputs.json: capture must be an object")
        if type(metadata.get("format_version")) is not int or metadata["format_version"] != 1:
            raise ValueError(
                f"unsupported main-inputs format_version {metadata.get('format_version')!r}"
            )
        config = MainConfig.model_validate(metadata["config"])
        records = metadata["records"]

        def text_of(record: dict[str, str]) -> str:
            try:
                return values[record["name"]].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{record['path']}: invalid UTF-8: {exc}") from exc

        bindings = properties_from_bytes(
            (r["path"], values[r["name"]]) for r in records if r["role"] == "properties"
        )
        parsed = []
        for source_record in records:
            if source_record["role"] not in ("jil", "python"):
                continue
            record = source_record
            if record["role"] == "python":
                record = next(
                    r for r in records if r["role"] == "generated" and r["path"] == record["path"]
                )
            original = record["path"]
            text = text_of(record)
            if any(r["role"] == "properties" for r in records):
                text, _ = substitute(text, bindings, file=original)
            jil = parse(text, file=original)
            parsed.append(jil)
        catalog = lower_catalog(parsed, permit_unknown=False)
        origins = {
            name: job.span.file for name, job in catalog.jobs.items() if job.span is not None
        }
        aliases = None
        for record in records:
            if record["role"] == "timezone_map":
                try:
                    aliases = parse_timezone_map(text_of(record))
                except ValueError as exc:
                    raise ValueError(f"{record['path']}: {exc}") from exc
        if resolve_timezone(config.estate.timezone, aliases) is None:
            raise ValueError(f"estate.timezone {config.estate.timezone!r} is not resolvable")
        exceptions = next((r for r in records if r["role"] == "exceptions"), None)
        if exceptions is None:
            raise ValueError("main-inputs.json: missing exceptions part")
        marked = parse_exceptions(text_of(exceptions), Path(exceptions["path"]))
        invalid = rule_l025(catalog, marked)
        if invalid:
            raise ValueError(
                f"{exceptions['path']}: invalid exception declaration: "
                + "; ".join(v.render() for v in invalid)
            )
        metadata = {**metadata, "origins": origins, "non_idempotent": dict(marked.reasons)}
        return Estate(
            catalog,
            estate_settings(config.estate.timezone, aliases, config.estate.semantics, catalog),
            tuple(parts),
            config.engine.environment,
            tuple(r["path"] for r in records if r["role"] == "properties"),
            tuple(parsed),
            str(Path(metadata["config_path"]).parent),
            metadata,
        )
    except (
        ValueError,
        KeyError,
        TypeError,
        JilParseError,
        LoweringError,
        PlaceholderError,
        IdempotenceError,
        StopIteration,
    ) as exc:
        raise RereadRefused([str(exc)]) from exc


def policy_report(estate: Estate) -> tuple[LintPolicy, LintReport]:
    policy = LintPolicy.model_validate(estate.metadata["config"]["lint"])
    report = lint_catalog(
        estate.catalog,
        base_tz=str(estate.settings["timezone"]),
        tz_aliases=estate.settings["timezone_aliases"],  # type: ignore[arg-type]
        exceptions=Exceptions(estate.metadata["non_idempotent"]),
    ).suppress(set(policy.ignore))
    return policy, report


def emit_policy(estate: Estate, *, refuse: bool = True) -> bool:
    policy, report = policy_report(estate)
    typer.echo(policy.describe(), err=True)
    for diagnostic in estate.metadata.get("diagnostics", []):
        typer.echo(diagnostic, err=True)
    for violation in report.violations:
        typer.echo(violation.render(), err=True)
    blocking = any(policy.blocks(v.severity) for v in report.violations)
    if blocking and refuse:
        raise typer.Exit(2)
    return blocking


def profile_of(estate: Estate) -> RuntimeProfile:
    launch = estate.metadata["launch"]
    return runtime_profile_from_cli(
        timezone=str(estate.settings["timezone"]),
        tz_aliases=cast(dict[str, str] | None, estate.settings["timezone_aliases"]),
        semantics=cast(dict[str, str], estate.settings["semantics"]),
        as_machine=launch["as_machine"],
        machine_policy=launch["machine_policy"],
    )


def encoded_parts(estate: Estate) -> dict[str, str]:
    return {name: base64.b64encode(data).decode("ascii") for name, data in estate.parts}


def decoded_parts(capture: Mapping[str, str]) -> Estate:
    return from_parts(
        [(name, base64.b64decode(data, validate=True)) for name, data in capture.items()]
    )


def fingerprint(estate: Estate) -> dict[str, str]:
    values = dict(estate.parts)
    return {
        r["path"]: hashlib.sha256(values[r["name"]]).hexdigest()
        for r in estate.metadata["records"]
        if r["role"] != "generated"
        and not (r["role"] == "exceptions" and estate.metadata["absent_exceptions"])
    }


def selected_config(
    ctx: typer.Context, path: str | None, legacy: bool, extra: Sequence[str] = ()
) -> Path | None:
    """Main config is the default only without a legacy source (DL-437)."""
    if path is None and legacy:
        return None
    forbidden = [
        "files",
        "estate_dir",
        "manifest",
        "properties",
        "environment",
        "timezone",
        "timezone_map",
        "semantics",
        "permit_unknown",
        "strict",
        "suppress",
        *extra,
    ]
    supplied = [
        name
        for name in forbidden
        if (source := ctx.get_parameter_source(name)) is not None and source.name == "COMMANDLINE"
    ]
    if supplied:
        raise typer.BadParameter(
            f"main config refuses command-line replacements: {', '.join(supplied)}"
        )
    return config_path(path) if path is not None else DEFAULT_CONFIG


def config_path(value: str) -> Path:
    """Validate a CLI spelling before pathlib normalizes it (DL-437)."""
    path = Path(value)
    try:
        joined(path, value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc
    return path


def pending_source_changes(estate: Estate) -> int:
    """Observe drift after startup; source drift cannot refuse a restart (DL-437)."""
    changed: set[str] = set()
    for original, expected in fingerprint(estate).items():
        try:
            actual = hashlib.sha256(input_bytes(original)).hexdigest()
        except RereadRefused:
            actual = "unreadable"
        if actual != expected:
            changed.add(original)
    metadata = estate.metadata
    config = MainConfig.model_validate(metadata["config"])
    path = Path(metadata["config_path"])
    patterns = list(config.estate.properties)
    if config.engine.environment is not None:
        patterns += config.estate.environments[config.engine.environment].properties
    for roles, patterns in [({"jil", "python"}, config.estate.include), ({"properties"}, patterns)]:
        baseline = {r["path"] for r in metadata["records"] if r["role"] in roles}
        try:
            changed |= baseline ^ set(expand(path, patterns, []))
        except (RereadRefused, ValueError):
            changed.add(str(path))
    if metadata["absent_exceptions"]:
        exception = next(r["path"] for r in metadata["records"] if r["role"] == "exceptions")
        if os.path.lexists(exception):
            changed.add(exception)
    return len(changed)
