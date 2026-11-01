"""What an engine runs under: the catalog's identity, the runtime profile, the marked set, the
engine settings, and the configuration row that holds them (DL-424).

The catalog hash is `reload_diff.catalog_hash_v2`: sha256 over the ss3.2 canonical form of
`CatalogIR` with `meta` projected to `{source_files}`; `tool_version` and `parsed_at` leave, spans
stay, so a patch release does not change the hash (DL-100).

`RuntimeProfile` holds the launch options that change interpretation or dispatch, as a typed
frozen model, so a field added later is hashed by construction (`runtime_hash`). The same JIL
launched with two `--timezone` values has one `catalog_hash` and two sets of UTC ticks.

The configuration row (`Configuration`) is what the store's `configs` table holds for one rev:
the canonical catalog, the profile and the marked set (`non-idempotent.toml`). Recovery and the
offline readers read the catalog and the switches from it, never from the estate files.

`EngineSettings` is `RUN_ROOT/engine.toml`: the stop grace, the snapshot cadence and the
retention window. The first `run` writes the defaults out; a flag overrides a value for one run
and is not written back.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dsl41.canon import CanonError, canonical_bytes, decode, hash_over
from dsl41.ir import CatalogIR
from dsl41.reload_diff import catalog_hash_v2, catalog_payload
from dsl41.runner_clock import EngineError
from dsl41.semantics import SemanticSwitches, check_overrides, resolve
from dsl41.timezones import alias_table

#: The FW default poll interval, in seconds as the CLI spells it (ss2.1).
FW_DEFAULT_INTERVAL_S: Final[float] = 60.0
#: The default stop grace: from TERM to KILL, and from KILL to the wrapper's record (DL-424).
GRACE_TERM_S: Final[float] = 10.0
GRACE_KILL_S: Final[float] = 5.0


def to_us(seconds: float) -> int:
    """ss2.1's conversion, exactly: `round(seconds * 1_000_000)`."""
    return round(seconds * 1_000_000)


# --------------------------------------------------------------- job fingerprints


def _strip_spans(value: Any) -> Any:
    """Drop every `span` key (the name of each `SourceSpan`/`CondSpan` field), so a job that
    only moved in its file fingerprints the same."""
    if isinstance(value, dict):
        return {k: _strip_spans(v) for k, v in value.items() if k != "span"}
    if isinstance(value, list):
        return [_strip_spans(v) for v in value]
    return value


def job_fingerprints(catalog: CatalogIR) -> dict[str, str]:
    """Per-job definition fingerprints: `catalog_hash`'s technique one level down.

    The estate-wide hash moves for any change anywhere, so it cannot mark one job's series. This
    fingerprints the resolved definition: an estate whose placeholders vary per run makes every
    job look changed, and the history's break line degrades to what `catalog_hash` said. The row
    carries both hashes so a reader can tell the two cases apart (DL-131)."""
    return {
        name: hashlib.sha256(
            json.dumps(_strip_spans(job_ir.model_dump(mode="json")), sort_keys=True).encode("utf-8")
        ).hexdigest()
        for name, job_ir in catalog.jobs.items()
    }


# ------------------------------------------------------------ runtime profile


#: How the one ambiguous machine verdict resolves (DL-49). The profile pins it; `runner_preflight`
#: imports this name (DL-209).
MachinePolicy = Literal["strict", "local-eligible"]


class RuntimeProfile(BaseModel):
    """ss2.1: the launch options that change interpretation or dispatch, as a typed frozen model.

    Durations are microseconds. The stop grace is not here: it is an engine setting
    (`EngineSettings`), the same for every estate the engine runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_tz: str = Field(default="UTC", min_length=1)
    tz_aliases: dict[str, str] = {}
    as_machine: tuple[str, ...] = ()

    @field_validator("as_machine", mode="before")
    @classmethod
    def _normalized_machines(cls, value: object) -> object:
        """Sort and de-duplicate (ss2.1), so two spellings of one machine set are one profile
        and one hash. Items are not coerced: a `7` is refused."""
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            if any(not isinstance(item, str) for item in items):
                raise ValueError("as_machine items must be strings")
            return tuple(sorted(set(items)))
        return value

    machine_policy: MachinePolicy = "strict"
    fw_default_interval_us: Annotated[int, Field(gt=0)] = to_us(FW_DEFAULT_INTERVAL_S)
    #: Semantic-switch overrides only, name -> value (runner-design ss8a, DL-252). Defaults live
    #: in `semantics.REGISTRY`, so an empty map is `{}` (ss3.2).
    semantics: dict[str, str] = {}

    @field_validator("semantics")
    @classmethod
    def _known_switches(cls, value: dict[str, str]) -> dict[str, str]:
        """Refuse an unknown name or out-of-set value; an explicit default is normalized away
        (`semantics.check_overrides`), so a restart naming it is not drift."""
        return check_overrides(value)


def runtime_hash(profile: RuntimeProfile) -> str:
    """sha256 over the ss3.2 canonical form of `profile`, `"sha256:..."`."""
    return hash_over(profile.model_dump(mode="json"))


def tz_aliases_of(profile: RuntimeProfile | None) -> dict[str, str] | None:
    """The SEM-35 alias table an engine runs under, as `resolve_timezone` takes it (DL-62,
    DL-151). Empty reads as absent (`timezones.alias_table`)."""
    return alias_table(None if profile is None else profile.tz_aliases)


def switches_of(profile: RuntimeProfile | None) -> SemanticSwitches:
    """The semantic switches an engine runs under (DL-252); registry defaults without a
    profile."""
    return resolve(None if profile is None else profile.semantics)


class OracleReading(TypedDict):
    """The keywords an `Oracle` reads a catalog under: base zone, alias table, switches."""

    default_tz: str | None
    tz_aliases: dict[str, str] | None
    semantics: SemanticSwitches


def oracle_reading(profile: RuntimeProfile | None) -> OracleReading:
    """How an `Oracle` reads a catalog: `Oracle(catalog, **oracle_reading(p))`.

    The one place a profile becomes an interpreter, so an input the profile gains reaches every
    replay at once (DL-151, DL-252, DL-253)."""
    return OracleReading(
        default_tz=None if profile is None else profile.default_tz,
        tz_aliases=tz_aliases_of(profile),
        semantics=switches_of(profile),
    )


def runtime_profile_from_cli(
    *,
    timezone: str | None = None,
    tz_aliases: Mapping[str, str] | None = None,
    as_machine: Iterable[str] = (),
    machine_policy: str = "strict",
    fw_default_interval_s: float = FW_DEFAULT_INTERVAL_S,
    semantics: Mapping[str, str] | None = None,
) -> RuntimeProfile:
    """The one CLI -> profile normalization (PR-15a).

    Omitted options take the stated defaults; an absent `--timezone` is `UTC`; seconds become
    microseconds by `round`. The model refuses an out-of-bound value at construction."""
    return RuntimeProfile(
        default_tz=timezone or "UTC",
        tz_aliases=dict(tz_aliases or {}),
        as_machine=tuple(sorted(set(as_machine))),
        machine_policy=machine_policy,  # type: ignore[arg-type]  # the Literal validates it
        fw_default_interval_us=to_us(fw_default_interval_s),
        semantics=dict(semantics or {}),
    )


def profile_drift(derived: RuntimeProfile, pinned: RuntimeProfile) -> list[str]:
    """Which profile fields moved, by name; the caller words the refusal (DL-137, DL-152)."""
    return sorted(
        name
        for name in type(derived).model_fields
        if getattr(derived, name) != getattr(pinned, name)
    )


def with_host_settings(
    profile: RuntimeProfile, as_machine: Iterable[str], machine_policy: str
) -> RuntimeProfile:
    """Override only this incarnation's host identity and placement policy (DL-437)."""
    return RuntimeProfile.model_validate(
        {
            **profile.model_dump(mode="json"),
            "as_machine": list(as_machine),
            "machine_policy": machine_policy,
        }
    )


# ------------------------------------------------------------ configuration row


class Configuration(BaseModel):
    """One configuration rev as the store's `configs` row holds it (DL-424): the catalog the
    engine runs, the profile it runs it under, and the jobs named in `non-idempotent.toml` with
    their reasons. The hashes are derived, never stored apart from what they hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog: CatalogIR
    profile: RuntimeProfile
    non_idempotent: dict[str, str] = {}
    source_capture: dict[str, str] = {}

    @property
    def catalog_hash(self) -> str:
        return catalog_hash_v2(self.catalog)

    @property
    def runtime_hash(self) -> str:
        return runtime_hash(self.profile)

    def to_bytes(self) -> bytes:
        """The row's canonical bytes."""
        payload: dict[str, Any] = {
            "catalog": catalog_payload(self.catalog),
            "profile": self.profile.model_dump(mode="json"),
            "non_idempotent": dict(self.non_idempotent),
        }
        if self.source_capture:
            payload.update(configuration_format_version=2, source_capture=self.source_capture)
        return canonical_bytes(payload)

    def digest(self) -> str:
        """The row's address: sha256 over its canonical bytes."""
        return "sha256:" + hashlib.sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, data: bytes, *, where: str) -> Configuration:
        """A row read back; a row that does not read as one is refused naming `where`."""
        try:
            raw: Any = decode(data)
            if not isinstance(raw, dict):
                raise TypeError("configuration must be an object")
            version = raw.get("configuration_format_version", 1)
            if type(version) is not int or version not in (1, 2):
                raise EngineError(f"{where}: unsupported configuration_format_version {version!r}")
            allowed = {"catalog", "profile", "non_idempotent", "configuration_format_version"}
            if version == 2:
                allowed |= {"configuration_format_version", "source_capture"}
                if not raw.get("source_capture"):
                    raise EngineError(f"{where}: configuration version 2 needs source_capture")
            if set(raw) - allowed:
                raise EngineError(
                    f"{where}: unknown configuration fields {sorted(set(raw) - allowed)}"
                )
            return cls(
                catalog=CatalogIR.model_validate(raw["catalog"]),
                profile=RuntimeProfile.model_validate(raw["profile"]),
                non_idempotent=dict(raw.get("non_idempotent") or {}),
                source_capture=dict(raw.get("source_capture") or {}),
            )
        except (CanonError, KeyError, TypeError, ValidationError) as exc:
            raise EngineError(f"{where}: unreadable configuration row: {exc}") from exc


# ------------------------------------------------------------ engine.toml


#: The settings file's name under the run root.
SETTINGS_FILE: Final[str] = "engine.toml"


class EngineSettings(BaseModel):
    """`RUN_ROOT/engine.toml` (DL-424): the engine's stop grace, snapshot cadence and journal
    retention. Seconds and days, as the file spells them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: from TERM to KILL in a stop ladder
    grace_term_s: Annotated[float, Field(gt=0)] = GRACE_TERM_S
    #: from KILL to the wrapper's record
    grace_kill_s: Annotated[float, Field(gt=0)] = GRACE_KILL_S
    #: the snapshot cadence when the journal moved
    snapshot_interval_s: Annotated[float, Field(gt=0)] = 300.0
    #: journal rows older than this and behind the latest snapshot are pruned
    retention_days: Annotated[int, Field(gt=0)] = 90

    def overridden(self, **values: float | int | None) -> EngineSettings:
        """These settings with each value that is not None replacing its field, for one run."""
        given = {name: value for name, value in values.items() if value is not None}
        return EngineSettings.model_validate({**self.model_dump(), **given})

    def to_toml(self) -> str:
        return "".join(f"{name} = {value!r}\n" for name, value in self.model_dump().items())


def load_settings(run_root: Path, *, create: bool = True) -> EngineSettings:
    """The run root's `engine.toml`, written with the defaults when it is absent and `create`
    holds; with `create` false an absent file reads as the defaults and nothing is written. A
    file that does not read is refused naming it."""
    path = run_root / SETTINGS_FILE
    if not path.exists():
        settings = EngineSettings()
        if not create:
            return settings
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(settings.to_toml())
            handle.flush()
            os.fsync(handle.fileno())
        return settings
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        return EngineSettings.model_validate(document)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise EngineError(f"{path}: {exc}") from exc
