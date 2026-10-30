"""A run root's identity and layout: what the engine pins, and where it keeps it.

The run root holds one period, period 1, for its whole life; the period number and segment
number stay in the artifacts as constants (DL-130, DL-133). Its semantics are `(catalog_hash,
runtime_hash, state_machine_version)`. This module owns the three identities, the two manifest
models, and the layout: the `period_root` sentinel, `wal/`, `periods/000001/` and the content-
addressed input bundle under `catalogs/`. `own_or_refuse` states the one ownership rule a root
is claimed under.

`catalog_hash` v2 is sha256 over the ss3.2 canonical form of `CatalogIR` with `meta` projected
to `{source_files}`; `tool_version` and `parsed_at` leave, spans stay, so a patch release does
not change the hash (DL-100). v1 is a retired dialect, refused by name and never recomputed
(DL-138). The version rides explicitly (`catalog_hash_version`); every gate compares hashes like
for like through `catalog_hash_for` and dispatches the version through
`check_catalog_hash_version`.

`source_bundle_hash` addresses the input bundle by bytes: inputs in command-line order, per file
`len(path) || path || len(bytes) || bytes`, both lengths 8-byte big-endian counts of UTF-8
bytes. Length framing keeps `["ab", "c"]` and `["a", "bc"]` apart. Order is hashed because
`catalog_hash` covers `CatalogMeta.source_files` and the spans.

`runtime_hash` covers the launch options that change interpretation or dispatch, as a typed
frozen model, so a field added later is hashed by construction. The same JIL launched with two
`--timezone` values has one `catalog_hash` and two sets of UTC ticks.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, TypedDict

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from dsl41.canon import (
    ARTIFACT_FORMAT_VERSION,
    CanonError,
    canonical_bytes,
    check_artifact_version,
    decode,
    hash_over,
    is_wire_int,
    require_artifact_version,
)
from dsl41.ir import CatalogIR
from dsl41.runner_clock import EngineError
from dsl41.runner_procid import durable_write, fsync_dir, mkdir_durable
from dsl41.semantics import SemanticSwitches, check_overrides, resolve
from dsl41.timezones import alias_table

if TYPE_CHECKING:
    from dsl41.ast_jil import JilFile

#: The one grammar of a stored digest; a native period never opens under another.
_HASH_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")


def is_hash_address(value: object) -> bool:
    """Whether `value` is a spelled sha256 address (DL-132)."""
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


#: The recipe `catalog_hash` names, carried on every `segment` and manifest (ss1.1).
CATALOG_HASH_VERSION: Final[int] = 2

#: Retired `catalog_hash` recipes: version -> the entry that retired it. Append-only
#: (docs/protocol-evolution.md ss6): an older root is owed a refusal that names what it holds.
RETIRED_CATALOG_HASH_VERSIONS: Final[dict[int, str]] = {1: "DL-138"}

#: The first period, segment and journal index.
GENESIS_PERIOD_ID: Final[int] = 1
GENESIS_SEGMENT_NO: Final[int] = 1
GENESIS_FIRST_INDEX: Final[int] = 1

#: ss2.1 defaults, in seconds as the CLI spells them. The profile and the engine's own defaults
#: (`runner_adapters`, `runner_startup`) read these, so a profile never disagrees with the engine.
FW_DEFAULT_INTERVAL_S: Final[float] = 60.0
CMD_GRACE_S: Final[float] = 10.0
RECONCILE_SETTLE_S: Final[float] = 5.0
SPAWN_WINDOW_S: Final[float] = 5.0
#: dormant: no code reads it in 2.0.0, and it stays in the profile so `runtime_hash` and the
#: manifest format do not move in this release
RETRY_HORIZON_S: Final[float] = 60.0


def to_us(seconds: float) -> int:
    """ss2.1's conversion, exactly: `round(seconds * 1_000_000)`."""
    return round(seconds * 1_000_000)


# --------------------------------------------------------------- catalog hash


def catalog_hash_v2(catalog: CatalogIR) -> str:
    """ss1.1: `"sha256:..."` over the ss3.2 canonical form of `catalog`, `meta` projected to
    `{source_files}`.

    The serializer refuses a condition nested deeper than about 250 grouping levels, so a run
    root holds none deeper (DL-351)."""
    try:
        payload = catalog.model_dump(mode="json")
    except ValueError as exc:
        if "depth exceeded" not in str(exc):
            raise
        raise EngineError(
            "the catalog nests a condition deeper than its hash serializes"
            " (about 250 levels of grouping); flatten the grouping or split"
            " the condition (period-model ss1.1)"
        ) from exc
    payload["meta"] = {"source_files": list(catalog.meta.source_files)}
    return hash_over(payload)


def _strip_spans(value: Any) -> Any:
    """Drop every `span` key (the name of each `SourceSpan`/`CondSpan` field), so a job that
    only moved in its file fingerprints the same."""
    if isinstance(value, dict):
        return {k: _strip_spans(v) for k, v in value.items() if k != "span"}
    if isinstance(value, list):
        return [_strip_spans(v) for v in value]
    return value


def job_fingerprints(catalog: CatalogIR) -> dict[str, str]:
    """Per-job definition fingerprints: `catalog_hash_v2`'s technique one level down.

    The estate-wide hash moves for any change anywhere, so it cannot mark one job's series or
    serve as a classifier's leaf test. The bundle holds post-placeholder JIL, so this
    fingerprints the resolved definition: an estate whose placeholders vary per run (as
    `examples/nightbank` bakes the run root into `profile` and the output files) makes every job
    look changed, and the break line degrades to what `catalog_hash` said. The row carries both
    hashes so a reader can tell the two cases apart.

    It answers "did this job's definition move", not "changed how"; `classify.py` builds that
    classification and names this the leaf test (period-model ss10.2, ss15; DL-131)."""
    return {
        name: hashlib.sha256(
            json.dumps(_strip_spans(job_ir.model_dump(mode="json")), sort_keys=True).encode("utf-8")
        ).hexdigest()
        for name, job_ir in catalog.jobs.items()
    }


def check_catalog_hash_version(version: int, *, where: str) -> None:
    """The one `catalog_hash` version dispatch (DL-138).

    The current version proceeds. A retired recipe refuses by name and cites its retiring entry;
    anything else refuses as unknown, because a root that predates a retirement and a corrupt or
    foreign one need different operator action (docs/protocol-evolution.md ss6).

    Callers: `check_segment_version`, `check_manifest_self_consistent`, `Journal.create` and
    `catalog_hash_at`."""
    if version == CATALOG_HASH_VERSION:
        return
    retired = RETIRED_CATALOG_HASH_VERSIONS.get(version)
    if retired is not None:
        raise EngineError(
            f"{where}: catalog_hash_version {version} is a RETIRED dialect, refused"
            f" by name since {retired} -- this binary reads {CATALOG_HASH_VERSION}"
            " only (docs/protocol-evolution.md ss6, ss8)"
        )
    raise EngineError(
        f"{where}: catalog_hash_version {version}: this binary implements"
        f" {CATALOG_HASH_VERSION} (period-model ss1.1)"
    )


def catalog_hash_at(version: int, catalog: CatalogIR) -> str:
    """`catalog`'s hash under the recipe `version` names.

    The version is dispatched before anything is recomputed (PR-08d): recomputing an unreadable
    recipe under the current one would report "the estate changed" over a log this build cannot
    read."""
    check_catalog_hash_version(version, where="catalog_hash")
    return catalog_hash_v2(catalog)


def catalog_hash_for(record: Mapping[str, Any], catalog: CatalogIR) -> str:
    """The hash to compare `record`'s `catalog_hash` against: the version the `segment` pins.

    The version is read strictly: a missing field, `"2"`, `true` or `2.7` is a malformed
    identity record and refuses."""
    version = record.get("catalog_hash_version")
    if not is_wire_int(version):
        raise EngineError(
            f"segment carries catalog_hash_version {version!r}: not an integer (period-model ss2.1)"
        )
    return catalog_hash_at(version, catalog)


# -------------------------------------------------------- the source bundle


class SourceFile(BaseModel):
    """One input: the path as given on the command line and the parsed post-placeholder text
    (byte-exact, F1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    text: str


def source_bundle_hash(sources: Sequence[SourceFile]) -> str:
    """ss1.1: sha256 over the inputs in command-line order, never sorted, each framed as
    `len(path) || path || len(bytes) || bytes` with 8-byte big-endian lengths."""
    accumulator = hashlib.sha256()
    for source in sources:
        for blob in (source.path.encode("utf-8"), source.text.encode("utf-8")):
            accumulator.update(len(blob).to_bytes(8, "big"))
            accumulator.update(blob)
    return "sha256:" + accumulator.hexdigest()


#: `source_bundle_hash` under a name no parameter shadows (bundle_source_paths).
source_bundle_hash_fn = source_bundle_hash

#: The address of the empty bundle, pinned by an engine started with no staged inputs (a
#: rehearsal, an embedder, the bisimulation harness); `dsl41 run` always stages one.
EMPTY_BUNDLE_HASH: Final[str] = source_bundle_hash(())


# ------------------------------------------------------------ runtime profile


#: How the one ambiguous machine verdict resolves (DL-49). The profile pins it into a period's
#: identity; `runner_preflight` imports this name (DL-209).
MachinePolicy = Literal["strict", "local-eligible"]


class RuntimeProfile(BaseModel):
    """ss2.1: the launch options that change interpretation or dispatch, as a typed frozen model.

    Every duration is microseconds with its resolved default and its own bound (zero is legal
    for the two `>= 0` windows, refused for the `> 0` intervals). Only `deadman_us` is null.

    Not here: `artifact_format_version`, which lives on the manifest that carries this profile,
    and the role->executor route table (the withdrawn HA plan's ss4, DL-189: a remap is not a
    re-baseline)."""

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
    execution_mode: Literal["tethered", "detached"] = "tethered"
    deadman_us: Annotated[int, Field(gt=0)] | None = None
    fw_default_interval_us: Annotated[int, Field(gt=0)] = to_us(FW_DEFAULT_INTERVAL_S)
    cmd_grace_us: Annotated[int, Field(gt=0)] = to_us(CMD_GRACE_S)
    reconcile_settle_us: Annotated[int, Field(ge=0)] = to_us(RECONCILE_SETTLE_S)
    spawn_window_us: Annotated[int, Field(ge=0)] = to_us(SPAWN_WINDOW_S)
    retry_horizon_us: Annotated[int, Field(gt=0)] = to_us(RETRY_HORIZON_S)
    #: Semantic-switch overrides only, name -> value (runner-design ss8a, DL-252). Defaults live
    #: in `semantics.REGISTRY`, so an empty map is `{}` (ss3.2).
    semantics: dict[str, str] = {}

    @field_validator("semantics")
    @classmethod
    def _known_switches(cls, value: dict[str, str]) -> dict[str, str]:
        """Refuse an unknown name or out-of-set value; an explicit default is normalized away
        (`semantics.check_overrides`), so a resume naming it is not drift."""
        return check_overrides(value)


def runtime_hash(profile: RuntimeProfile) -> str:
    """sha256 over the ss3.2 canonical form of `profile`, `"sha256:..."`."""
    return hash_over(profile.model_dump(mode="json"))


def default_tz_of(profile: "RuntimeProfile | None") -> str | None:
    """The base zone a period runs under, from its own pin (DL-155, DL-253).

    The oracle reads it for a job with no `timezone:`: start-time slots, absolute must times and
    run_window. None when no pin is held, which reads the engine clock."""
    return None if profile is None else profile.default_tz


def tz_aliases_of(profile: "RuntimeProfile | None") -> dict[str, str] | None:
    """The SEM-35 alias table a period runs under, as `resolve_timezone` takes it, from the
    period's own pin (DL-62, DL-151).

    The catalog and the WAL carry no table, so the profile is the only record; a name only the
    table resolves is `OracleError` without it. Empty reads as absent (`timezones.alias_table`)."""
    return alias_table(None if profile is None else profile.tz_aliases)


def switches_of(profile: "RuntimeProfile | None") -> SemanticSwitches:
    """The semantic switches a period runs under, from its own pin (DL-252); registry defaults
    without a profile."""
    return resolve(None if profile is None else profile.semantics)


class OracleReading(TypedDict):
    """The keywords an `Oracle` reads a period under: base zone, alias table, switches."""

    default_tz: str | None
    tz_aliases: dict[str, str] | None
    semantics: SemanticSwitches


def oracle_reading(profile: "RuntimeProfile | None") -> OracleReading:
    """How an `Oracle` reads a period: `Oracle(catalog, **oracle_reading(p))`.

    The one place a profile becomes an interpreter, so an input the pin gains reaches every
    replay at once (DL-151, DL-252, DL-253)."""
    return OracleReading(
        default_tz=default_tz_of(profile),
        tz_aliases=tz_aliases_of(profile),
        semantics=switches_of(profile),
    )


def runtime_profile_from_cli(
    *,
    timezone: str | None = None,
    tz_aliases: Mapping[str, str] | None = None,
    as_machine: Iterable[str] = (),
    machine_policy: str = "strict",
    detached: bool = False,
    deadman_s: float | None = None,
    fw_default_interval_s: float = FW_DEFAULT_INTERVAL_S,
    cmd_grace_s: float = CMD_GRACE_S,
    reconcile_settle_s: float = RECONCILE_SETTLE_S,
    spawn_window_s: float = SPAWN_WINDOW_S,
    retry_horizon_s: float = RETRY_HORIZON_S,
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
        execution_mode="detached" if detached else "tethered",
        deadman_us=None if deadman_s is None else to_us(deadman_s),
        fw_default_interval_us=to_us(fw_default_interval_s),
        cmd_grace_us=to_us(cmd_grace_s),
        reconcile_settle_us=to_us(reconcile_settle_s),
        spawn_window_us=to_us(spawn_window_s),
        retry_horizon_us=to_us(retry_horizon_s),
        semantics=dict(semantics or {}),
    )


# --------------------------------------------------------------- the manifests


class StagedManifest(BaseModel):
    """ss2.1: what the launcher pins about a run root, nothing the engine owns. Genesis builds
    one and installs it.

    `strict=True` (DL-168) gives the wire's types on every top-level field of a lax
    `model_validate`, beside `_read_artifact`'s strict parse. The nested `runtime_profile`
    validates under its own config, so `commit` passes the instance."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    artifact_format_version: int = ARTIFACT_FORMAT_VERSION
    catalog_hash: str
    catalog_hash_version: int = CATALOG_HASH_VERSION
    source_bundle_hash: str
    runtime_profile: RuntimeProfile
    runtime_hash: str
    state_machine_version: int

    def commit(
        self,
        *,
        period_id: int,
        baseline_id: str,
        clock_domain: str,
        segment_no: int,
        first_index: int,
    ) -> Manifest:
        """The engine's half (ss2.1): the staged fields plus the five only the engine knows.

        `dict(self)`, not `model_dump()`, keeps `runtime_profile` a validated instance
        (DL-168)."""
        return Manifest(
            **dict(self),
            period_id=period_id,
            baseline_id=baseline_id,
            clock_domain=clock_domain,
            segment_no=segment_no,
            first_index=first_index,
        )


class Manifest(StagedManifest):
    """ss2.1: the committed period manifest, the staged fields plus the engine-derived ones.
    Both models forbid extras, so a committed manifest never validates as a staged one."""

    period_id: int
    baseline_id: str
    clock_domain: str
    segment_no: int
    first_index: int


def genesis_manifest(
    catalog: CatalogIR,
    *,
    clock_domain: str,
    state_machine_version: int,
    staged: StagedManifest | None = None,
    baseline_id: str | None = None,
) -> Manifest:
    """The committed manifest of period 1.

    `staged` is what the launcher pinned; without it the default runtime profile over the empty
    bundle applies. `baseline_id` is minted here unless given, because genesis has nothing to
    take it from (DL-117)."""
    staged = staged or stage_manifest(
        catalog,
        source_bundle_hash=EMPTY_BUNDLE_HASH,
        profile=RuntimeProfile(),
        state_machine_version=state_machine_version,
    )
    return staged.commit(
        period_id=GENESIS_PERIOD_ID,
        baseline_id=baseline_id or str(uuid.uuid4()),
        clock_domain=clock_domain,
        segment_no=GENESIS_SEGMENT_NO,
        first_index=GENESIS_FIRST_INDEX,
    )


def stage_manifest(
    catalog: CatalogIR,
    *,
    source_bundle_hash: str,
    profile: RuntimeProfile,
    state_machine_version: int,
) -> StagedManifest:
    """A `StagedManifest` over `catalog` under `profile`; the identities are computed only
    here, so the manifest and its `segment` record agree."""
    return StagedManifest(
        catalog_hash=catalog_hash_v2(catalog),
        source_bundle_hash=source_bundle_hash,
        runtime_profile=profile,
        runtime_hash=runtime_hash(profile),
        state_machine_version=state_machine_version,
    )


# ------------------------------------------------------------------- layout


def bundle_dir(run_root: Path, source_bundle_hash: str) -> Path:
    """`<root>/catalogs/<digest>/`, content-addressed by bytes (ss1.1).

    The name is the digest's hex half: a colon in a copied path reads as a remote host to
    `rsync` and `scp`."""
    return run_root / "catalogs" / source_bundle_hash.split(":")[-1]


def period_dir(run_root: Path, period_id: int) -> Path:
    """`<root>/periods/<period_id zero-padded to 6>/` (ss1.1)."""
    return run_root / "periods" / f"{period_id:06d}"


# -------------------------------------------------------- the sentinel and wal

#: ss1.1: the permanent one-line sentinel every periodized root carries. The name is deliberate:
#: a binary that predates the period model refuses `run` on a root holding `journal.jsonl`. The
#: file is never absent, so a used root never reads as unused.
SENTINEL_NAME: Final[str] = "journal.jsonl"

#: ss1.1: `wal/000001.jsonl` is period 1, `000002` is period 2 (I1: one
#: period, one segment, one number).
WAL_DIR: Final[str] = "wal"

#: The sentinel's `see`: where the records went. A reader follows it instead of assuming it.
WAL_POINTER: Final[Literal["wal/"]] = "wal/"

_WAL_NAME: Final = re.compile(r"^(\d{6})\.jsonl$")


class Sentinel(BaseModel):
    """ss1.1's `period_root` record: one line, one schema, every periodized root.

    It names the owning estate and where the records are, and proves that this estate owns the
    root. `claim_id` is a 1.x field, always null in 2.0.0."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rec: Literal["period_root"] = "period_root"
    artifact_format_version: int = ARTIFACT_FORMAT_VERSION
    estate_id: str = Field(min_length=1)
    see: Literal["wal/"] = WAL_POINTER
    #: a 1.x field: always null in 2.0.0
    claim_id: str | None = None


def split_run_dir(name: str) -> tuple[str, int] | None:
    """`<job>.<run_number>`, split at the last dot (a job name may hold one). Canonical spelling
    only: `b.01` would alias `b.1` and let a directory this estate never wrote answer for a
    run's evidence (ss11a; DL-137). The resume sweep uses it."""
    job, dot, tail = name.rpartition(".")
    if not dot or not job or not tail.isdigit():
        return None
    if str(int(tail)) != tail:
        return None
    return (job, int(tail))


def sentinel_path(run_root: Path) -> Path:
    return run_root / SENTINEL_NAME


def _is_period_root_line(line: bytes) -> bool:
    """Whether `line` is a `period_root` record at all, asked with ss3.2's ingress gates off.

    It picks the refusal an operator gets; `Sentinel` still validates every field."""
    try:
        payload = json.loads(line)
    except (ValueError, RecursionError):
        return False
    return isinstance(payload, dict) and payload.get("rec") == "period_root"


def read_sentinel(run_root: Path) -> Sentinel | None:
    """The root's sentinel, or None when this root has none.

    A `period_root` record this binary cannot read is an `EngineError` naming it. A
    `journal.jsonl` that is not a `period_root` line is no sentinel, and the caller
    (`claim_root`) names what it found (DL-138)."""
    path = sentinel_path(run_root)
    try:
        with path.open("rb") as handle:
            first = handle.readline()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EngineError(f"{path}: unreadable: {exc}") from exc
    try:
        payload = decode(first)
    except CanonError as exc:
        if _is_period_root_line(first):
            # an unreadable `period_root` record (unknown artifact_format_version, duplicate
            # key, float) refuses by name (DL-151)
            raise EngineError(f"{path}: not a sentinel this binary can read ({exc})") from exc
        return None  # not canonical JSON: whatever it is, it is not a sentinel
    if not isinstance(payload, dict) or payload.get("rec") != "period_root":
        return None  # a first line that is not `period_root` is not a sentinel
    try:
        require_artifact_version(payload)  # DL-157
        return Sentinel.model_validate(payload)
    except (CanonError, ValidationError) as exc:
        raise EngineError(f"{path}: not a sentinel this binary can read ({exc})") from exc


def write_sentinel(run_root: Path, sentinel: Sentinel) -> Path:
    """The sentinel, ss3.2-canonical, by the durable-write liturgy (ss11 step 3).

    It is not create-only: `own_or_refuse` decides under `leader.lock` whether this call may
    happen."""
    path = sentinel_path(run_root)
    durable_write(str(path), canonical_bytes(sentinel.model_dump(mode="json")) + b"\n")
    fsync_dir(run_root)
    return path


def wal_path(run_root: Path, segment_no: int) -> Path:
    """`<root>/wal/<segment_no zero-padded to 6>.jsonl` (ss1.1)."""
    return run_root / WAL_DIR / f"{segment_no:06d}.jsonl"


def wal_segments(run_root: Path) -> list[int]:
    """Every segment this root holds, in order.

    A file under `wal/` that is not `<six digits>.jsonl` is foreign and refused (I1: the name
    is the period number). Dot-files are the liturgy's temporaries and are skipped."""
    directory = run_root / WAL_DIR
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise EngineError(f"{directory}: unreadable: {exc}") from exc
    segments: list[int] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        matched = _WAL_NAME.match(entry.name)
        if matched is None:
            raise EngineError(
                f"{entry}: not a segment file -- `wal/` holds `<six digits>.jsonl`"
                " and nothing else (period-model ss1.1, I1)"
            )
        segments.append(int(matched.group(1)))
    return sorted(segments)


def active_wal(run_root: Path) -> Path:
    """The segment an appender opens: the newest, or segment 1 before genesis writes one."""
    segments = wal_segments(run_root)
    return wal_path(run_root, segments[-1] if segments else GENESIS_SEGMENT_NO)


def resolve_wal(path: Path | str) -> Path:
    """The WAL a caller means by `path`: an estate root, its sentinel, or a segment. The
    sentinel's `see` is followed only here (ss1.1)."""
    path = Path(path)
    if path.is_dir():
        return estate_wal(path)
    if path.name == SENTINEL_NAME and read_sentinel(path.parent) is not None:
        return active_wal(path.parent)
    return path


def estate_wal(run_root: Path) -> Path:
    """The appender's segment: `wal/<newest>.jsonl` on a periodized root, `journal.jsonl`
    without a sentinel (DL-138)."""
    return active_wal(run_root) if read_sentinel(run_root) is not None else sentinel_path(run_root)


def root_of_wal(wal: Path) -> Path:
    """The estate root a WAL file belongs to: the inverse of `estate_wal` (DL-135).

    A periodized segment is `<root>/wal/<n>.jsonl`; a sentinel-less root's file is
    `<root>/journal.jsonl`; the parent's name tells them apart."""
    wal = Path(wal)
    return wal.parent.parent if wal.parent.name == WAL_DIR else wal.parent


def root_is_unused(run_root: Path) -> bool:
    """Whether nothing exists at the sentinel's name; `run` asks it before staging a period."""
    return not sentinel_path(run_root).exists()


def own_or_refuse(
    *, exists: bool, ours: bool, what: str, holder: str
) -> Literal["create", "resume"]:
    """ss1.1's one ownership rule, for every root and anchor:

    > absent -> create. Exact same estate and exact same incomplete
    > transaction -> resume. Anything else -> refuse.

    "Fresh root" is not checkable: E1 could take a dormant estate E2's free `leader.lock` and
    overwrite its sentinel while E2's anchor still named the root (PR-01c). An existing anchor
    is an existing estate whose detached work may be alive (PR-01b). The caller decides what
    "ours" means, because it knows which transaction it resumes."""
    if not exists:
        return "create"
    if ours:
        return "resume"
    raise EngineError(not_ours(what=what, holder=holder))


def not_ours(*, what: str, holder: str) -> str:
    """`own_or_refuse`'s refusal text; the anchor raises it for the `anchor_exists` code
    (DL-336)."""
    return (
        f"{what} already exists and is not ours ({holder}): absent creates,"
        " our own incomplete transaction resumes, anything else refuses"
        " (period-model ss1.1)"
    )


def write_bundle(run_root: Path, sources: Sequence[SourceFile]) -> str:
    """Materialize the immutable input bundle and return its address.

    A complete directory is reused, never rewritten. Each file is the post-placeholder JIL,
    byte-exact (F1), beside `sources.json`, the ordered vector of original paths and sha256
    that a reopening reads. Complete means `sources.json` exists: it is written last. The
    bundle is assembled in a temp directory and renamed in; a concurrent same-bytes writer is
    not an error."""
    address = source_bundle_hash(sources)
    directory = bundle_dir(run_root, address)
    # the first write into a fresh run root (the CLI stages before genesis), so every created
    # directory, the run root included, is made durable here
    mkdir_durable(str(directory.parent))
    os.chmod(directory.parent, 0o700)
    # Everything, the completeness check included, runs under catalogs/.lock and the publisher
    # fsyncs before releasing. A crashed writer's leftover must be repairable, a complete
    # bundle must never be deleted, and "complete" must mean durable; a lock-free step breaks
    # one of the three.
    lock_fd = os.open(directory.parent / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if (directory / "sources.json").exists():
            return address  # a same-bytes writer won, and fsynced before releasing
        staging = directory.parent / f".{directory.name}.{os.getpid()}.tmp"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        os.chmod(staging, 0o700)
        vector: list[dict[str, str]] = []
        for name, source in zip(_bundle_names(sources), sources, strict=True):
            durable_write(str(staging / name), source.text.encode("utf-8"))
            vector.append(
                {
                    "file": name,
                    "path": source.path,
                    "sha256": "sha256:" + hashlib.sha256(source.text.encode("utf-8")).hexdigest(),
                }
            )
        _write_canonical_file(
            staging / "sources.json",
            {
                "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                "source_bundle_hash": address,
                "sources": vector,
            },
        )
        shutil.rmtree(directory, ignore_errors=True)  # provably incomplete under the lock
        os.rename(staging, directory)
        fsync_dir(directory.parent)
        fsync_dir(directory.parent.parent)  # run_root's entry for catalogs/
    finally:
        os.close(lock_fd)  # closing releases the flock
    return address


def bundle_source_paths(run_root: Path, source_bundle_hash: str) -> list[Path]:
    """The bundle's stored files in `sources.json` order, the command-line order the hash
    covers; verified by `_bundle_entries`."""
    return [path for path, _ in _bundle_entries(run_root, source_bundle_hash)]


def bundle_sources(run_root: Path, source_bundle_hash: str) -> list[SourceFile]:
    """The bundle's inputs as given: the original path each came under, and its
    post-placeholder text. A boundary re-parses these (DL-133).

    Spans name their source file, so parsing under the stored names would never hash back to
    the staged pin; under the original names the same bytes reproduce the hash on a host
    without the original files (ss7 phase 1). `load_catalog_from_manifest` is not hash-gated
    for that reason."""
    return [source for _, source in _bundle_entries(run_root, source_bundle_hash)]


def _bundle_entries(run_root: Path, source_bundle_hash: str) -> list[tuple[Path, SourceFile]]:
    """Every stored file with the input it stands for, verified: each name is a plain
    basename, each file's bytes match the recorded sha256, and the framing hash over
    (recorded paths, stored bytes) reproduces the address."""
    directory = bundle_dir(run_root, source_bundle_hash)
    payload = _read_canonical_file(directory / "sources.json")
    if payload is None:
        raise EngineError(f"{directory}: no readable sources.json (period-model ss1.1)")
    try:
        require_artifact_version(payload)
    except CanonError:
        raise EngineError(
            f"{directory}/sources.json carries artifact_format_version"
            f" {payload.get('artifact_format_version')!r}"
        ) from None
    if payload.get("source_bundle_hash") != source_bundle_hash:
        # the vector's claim must be the address it sits under, even when the files verify
        raise EngineError(
            f"{directory}/sources.json claims {payload.get('source_bundle_hash')!r}"
            f" but sits at {source_bundle_hash}"
        )
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise EngineError(f"{directory}/sources.json carries no sources")
    paths: list[Path] = []
    rebuilt: list[SourceFile] = []
    for entry in sources:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise EngineError(f"{directory}/sources.json: a source names no file")
        name = entry["file"]
        if os.sep in name or name in (".", "..", "sources.json") or not name:
            raise EngineError(f"{directory}/sources.json: unsafe stored name {name!r}")
        path = directory / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise EngineError(f"{path}: unreadable bundle file: {exc}") from exc
        recorded = entry.get("sha256")
        actual = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        if recorded != actual:
            raise EngineError(
                f"{path}: bytes hash {actual} but sources.json records {recorded!r}"
                " -- the bundle is not the one its address names"
            )
        rebuilt.append(SourceFile(path=str(entry.get("path", "")), text=text))
        paths.append(path)
    if source_bundle_hash != source_bundle_hash_fn(rebuilt):
        raise EngineError(
            f"{directory}: the stored files do not reproduce the address"
            f" {source_bundle_hash} -- the bundle is not the one its address names"
        )
    return list(zip(paths, rebuilt, strict=True))


def _bundle_names(sources: Sequence[SourceFile]) -> list[str]:
    """Stored filenames: the input's basename, disambiguated by position when two share one.
    A pure function of (paths, order)."""
    names: list[str] = []
    used: set[str] = {"sources.json"}  # the metadata name is never a source's
    for position, source in enumerate(sources, start=1):
        base = Path(source.path).name or "estate.jil"
        name, suffix = base, position
        while name in used:
            name = f"{suffix:02d}-{base}"
            suffix += 1
        used.add(name)
        names.append(name)
    return names


def write_period_manifest(run_root: Path, manifest: Manifest) -> Path:
    """`periods/<N>/manifest.json`, ss3.2-canonical, by the durable-write liturgy.

    Mode 0700 on the directory and 0600 on the file, on DL-66's rule."""
    # checked at the write too: `tz_aliases` is a mutable dict on a frozen model, and a profile
    # mutated after hashing would commit a false pin
    check_manifest_self_consistent(manifest, f"periods/{manifest.period_id:06d}/manifest.json")
    directory = period_dir(run_root, manifest.period_id)
    directory.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(directory.parent, 0o700)
    directory.mkdir(exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / "manifest.json"
    _write_canonical_file(path, manifest.model_dump(mode="json"))
    # the liturgy fsyncs the file and its directory; the entries for periods/<N>/ and periods/
    # must also survive a power loss
    fsync_dir(directory.parent)
    fsync_dir(run_root)
    return path


def check_manifest_self_consistent(manifest: StagedManifest, where: str) -> None:
    """The manifest agrees with itself (PR-22), checked at every write and read.

    `runtime_hash` is the hash of the profile it carries, because the segment pins only the
    hash. `artifact_format_version` is the one this binary writes. `catalog_hash_version` is
    the current recipe."""
    try:
        check_artifact_version({"artifact_format_version": manifest.artifact_format_version})
    except CanonError:
        raise EngineError(
            f"{where}: artifact_format_version {manifest.artifact_format_version}:"
            f" this binary writes {ARTIFACT_FORMAT_VERSION}"
        ) from None
    # D4 goes through the one dispatcher (DL-138), so the refusal reads the same everywhere
    check_catalog_hash_version(manifest.catalog_hash_version, where=where)
    for field in ("catalog_hash", "source_bundle_hash", "runtime_hash"):
        if not is_hash_address(getattr(manifest, field)):
            raise EngineError(
                f"{where}: {field} {getattr(manifest, field)!r} is not a sha256 address"
            )
    expected = runtime_hash(manifest.runtime_profile)
    if manifest.runtime_hash != expected:
        raise EngineError(
            f"{where}: runtime_hash {manifest.runtime_hash} is not the hash of the"
            f" profile it carries ({expected}) -- the manifest disagrees with itself"
        )


def require_manifest_fields(
    payload: Mapping[str, Any], model: type[StagedManifest], *, where: str
) -> None:
    """Every field of `model` and of its nested `runtime_profile` is on the wire, for a
    committed and a staged manifest (DL-252); a defaulted pin is no pin."""
    missing = sorted(set(model.model_fields) - set(payload))
    if missing:
        raise EngineError(f"{where}: missing {', '.join(missing)}")
    nested = payload.get("runtime_profile")
    if isinstance(nested, dict):
        gone = sorted(set(RuntimeProfile.model_fields) - set(nested))
        if gone:
            # a profile field restored from its default would hash back and pin nothing
            raise EngineError(f"{where}: runtime_profile missing {', '.join(gone)}")


def read_or_none(path: Path) -> bytes | None:
    """The file's bytes, or None on ENOENT; any other OSError refuses, because an unreadable
    file must not read as an empty root."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EngineError(f"{path}: unreadable: {exc}") from exc


def read_period_manifest(run_root: Path, period_id: int = GENESIS_PERIOD_ID) -> Manifest | None:
    """The committed manifest, or None when this root has none (DL-113 decision 5).

    A missing artifact degrades; a present but unreadable one raises an `EngineError` naming
    the file. The caller says what "none" means: `runner_history` discriminates the retired
    `manifest/` layout, `runner_startup` refuses a segment root that lost its pin."""
    path = period_dir(run_root, period_id) / "manifest.json"
    try:
        raw = read_or_none(path)
        if raw is None:
            return None
        payload = decode(raw)  # the ss3.2 ingress: dup keys, floats, surrogates
        if not isinstance(payload, dict):
            raise EngineError(f"{path}: not a JSON object")
        require_manifest_fields(payload, Manifest, where=str(path))
        # strict JSON: no string-to-int or bool-to-int coercion
        manifest = Manifest.model_validate_json(raw, strict=True)
        check_manifest_self_consistent(manifest, str(path))
        return manifest
    except (CanonError, ValidationError) as exc:
        raise EngineError(f"{path}: not a period manifest this binary can read ({exc})") from exc


def _write_canonical_file(path: Path, payload: Mapping[str, Any]) -> None:
    durable_write(str(path), canonical_bytes(payload))


def _read_canonical_file(path: Path) -> dict[str, Any] | None:
    """The document, or None on ENOENT; a file that does not decode raises."""
    raw = read_or_none(path)
    if raw is None:
        return None
    payload = decode(raw)
    if not isinstance(payload, dict):
        raise EngineError(f"{path}: not a JSON object")
    return payload


# ------------------------------------------------------------ the record


def segment_record(
    manifest: Manifest,
    *,
    estate_id: str,
    at: datetime,
    opens_from_seal: Mapping[str, Any] | None = None,
    reclaimed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """ss2.1's `segment`, the first record of every segment.

    It is self-describing: period, catalog and semantics need no earlier file. `dsl41_version`
    is not in it, because it is per-process, rides on `leader`, and a patch release must not
    move these bytes.

    `opens_from_seal` is null on segment 1, the only segment a 2.0.0 root holds; the field
    stays in the record format."""
    return {
        "rec": "segment",
        "segment_no": manifest.segment_no,
        "estate_id": estate_id,
        "period_id": manifest.period_id,
        "baseline_id": manifest.baseline_id,
        "catalog_hash": manifest.catalog_hash,
        "catalog_hash_version": manifest.catalog_hash_version,
        "source_bundle_hash": manifest.source_bundle_hash,
        "runtime_hash": manifest.runtime_hash,
        "state_machine_version": manifest.state_machine_version,
        "clock_domain": manifest.clock_domain,
        "first_index": manifest.first_index,
        # the boundary's field; genesis passes none
        "opens_from_seal": dict(opens_from_seal) if opens_from_seal is not None else None,
        # 1.x break-glass fields (ss1.3, ss11): always null in 2.0.0, kept in the record format
        "reclaimed": dict(reclaimed) if reclaimed is not None else None,
        "trust_unaudited": None,
        "at": at.isoformat(),
    }


def _opens_from_seal_ok(value: object) -> bool:
    """ss2.1's `{period_id, digest}`, exactly: the two fields recovery selects the sidecar by
    (DL-132)."""
    return (
        isinstance(value, dict)
        and set(value) == {"period_id", "digest"}
        and is_wire_int(value["period_id"])
        and is_hash_address(value["digest"])
    )


#: The ss2.1 segment schema: required keys and exact-type checks (`true` never passes for `1`).
_str = lambda v: isinstance(v, str)  # noqa: E731
_SEGMENT_SCHEMA: Final[dict[str, Any]] = {
    "segment_no": is_wire_int,
    "estate_id": _str,
    "period_id": is_wire_int,
    "baseline_id": _str,
    "catalog_hash": is_hash_address,
    # type only; which recipes are readable is the D4 dispatcher's question. JSON loads `2.0`
    # as a float equal to 2, hence the exact-int check.
    "catalog_hash_version": is_wire_int,
    "source_bundle_hash": is_hash_address,
    "runtime_hash": is_hash_address,
    "state_machine_version": is_wire_int,
    "clock_domain": _str,
    # >= 1: a forged 0 would stop the backfill's positional containment here (I2)
    "first_index": lambda v: is_wire_int(v) and v >= 1,
    "opens_from_seal": lambda v: v is None or _opens_from_seal_ok(v),
    "reclaimed": lambda v: v is None or isinstance(v, dict),
    "trust_unaudited": lambda v: v is None or isinstance(v, dict),
    "at": _str,
}


#: The ss2.1 segment field names (DL-136).
SEGMENT_FIELDS = frozenset(_SEGMENT_SCHEMA)


def check_segment_version(record: Mapping[str, Any]) -> None:
    """The opening's `catalog_hash_version`, read exactly and dispatched before any other
    check (DL-138, D4).

    The schema describes the current dialect only. A version-1 opening carries a bare hexdigest
    `catalog_hash`, which the grammar check would call malformed instead of retired, so the
    version verdict comes first.

    A missing field, `true`, `"2"` or `2.0` cannot say which recipe it means and is malformed;
    those two messages are the schema's own, spelled here because this runs first."""
    if "catalog_hash_version" not in record:
        raise EngineError("segment record missing catalog_hash_version (period-model ss2.1)")
    version = record["catalog_hash_version"]
    if not is_wire_int(version):
        raise EngineError(
            f"segment record: catalog_hash_version is {version!r} (period-model ss2.1)"
        )
    check_catalog_hash_version(version, where="segment record")


def check_record_fields(
    record: Mapping[str, Any],
    schema: Mapping[str, Callable[[Any], bool]],
    *,
    where: str,
    cite: str,
) -> None:
    """The exact-schema walk for ss2 records (DL-137).

    Refusals, in order: an unknown key (`rec` is never unknown), then per field in schema
    order, missing before mistyped. `where` names the record kind and `cite` its defining
    section. Caller: `check_segment_record` (ss2.1).

    It validates the fields of a record whose kind is known; `runner_journal.check_record`
    dispatches the kind (DL-138 D1)."""
    extras = sorted(set(record) - set(schema) - {"rec"})
    if extras:
        raise EngineError(f"{where} carries unknown {', '.join(extras)}")
    for key, check in schema.items():
        if key not in record:
            raise EngineError(f"{where} missing {key} ({cite})")
        if not check(record[key]):
            raise EngineError(f"{where}: {key} is {record[key]!r} ({cite})")


def check_segment_record(record: Mapping[str, Any]) -> None:
    """A `segment` is a ss2.1 segment: every field present with its exact type.

    It runs where the record is first read (`read_journal`), so no later gate meets
    `state_machine_version: true` as 1. `read_journal` refuses the retired `header` by name
    before this runs (DL-138)."""
    # D4 first: the recipe decides whether this schema describes the record at all
    check_segment_version(record)
    check_record_fields(record, _SEGMENT_SCHEMA, where="segment record", cite="period-model ss2.1")
    # I1: segment_no is period_id (ss3.4; genesis is 1/1), both positive
    if record["segment_no"] < 1 or record["segment_no"] != record["period_id"]:
        raise EngineError(
            f"segment {record['segment_no']} declares period {record['period_id']}:"
            " one period, one segment, same number (period-model ss2.1, I1)"
        )
    # ss2.1: `opens_from_seal` is null on segment 1 and non-null on every later segment
    opens = record["opens_from_seal"] is not None
    if opens != (record["segment_no"] > 1):
        raise EngineError(
            f"segment {record['segment_no']} with opens_from_seal"
            f" {record['opens_from_seal']!r}: segment 1 opens no seal and every later"
            " segment opens one (period-model ss2.1)"
        )
    link = record.get("opens_from_seal")
    if isinstance(link, Mapping):
        named = link.get("period_id")
        if named != record["period_id"] - 1:
            # a segment opens exactly the seal that closed its predecessor; any other is a graft
            raise EngineError(
                f"segment {record['segment_no']}: opens_from_seal names period"
                f" {named!r} but the predecessor is {record['period_id'] - 1}"
                " (period-model ss2.1)"
            )


#: The one record kind a journal opens with; `runner_journal.RETIRED_RECS` refuses the retired
#: `header` by name (DL-138).
OPENING_RECS: Final[tuple[str, ...]] = ("segment",)


def is_opening(record: Mapping[str, Any]) -> bool:
    return record.get("rec") in OPENING_RECS


def opening_at(record: Mapping[str, Any]) -> datetime:
    """When the segment was opened."""
    stamp = record.get("at")
    if not isinstance(stamp, str):
        raise EngineError(f"opening record carries no timestamp: {record.get('rec')!r}")
    return datetime.fromisoformat(stamp)


def _field_of(source: object, field: str) -> Any:
    """One field of either side. A mapping is read with `.get`, so an absent key is a value;
    a model with `getattr` and no default, so an undeclared field is a caller bug."""
    return source.get(field) if isinstance(source, Mapping) else getattr(source, field)


def disagreements(left: object, right: object, fields: Iterable[str]) -> list[tuple[str, Any, Any]]:
    """The field-by-field comparison walk (DL-137).

    Returns `(field, left value, right value)` for every disagreeing field, in the order
    `fields` gives. It does not word the refusal; the caller does. Either side may be a model
    or a mapping. It does not short-circuit, so a reporting caller gets all the fields."""
    found: list[tuple[str, Any, Any]] = []
    for field in fields:
        lhs, rhs = _field_of(left, field), _field_of(right, field)
        if lhs != rhs:
            found.append((field, lhs, rhs))
    return found


#: The fields a committed manifest and its segment record must agree on (PR-22): every shared one.
_SHARED_FIELDS = tuple(
    sorted(set(Manifest.model_fields) & SEGMENT_FIELDS)
)  # derived (DL-137): a field added to Manifest and the schema is checked by default


def check_manifest_against_segment(manifest: Manifest, segment: Mapping[str, Any]) -> None:
    """PR-22's U4 half: at resume the committed manifest is checked against the record that
    names it. The refusal names each disagreeing field and both values."""
    disagree = [
        f"{field}: manifest {mine!r} vs segment {theirs!r}"
        for field, mine, theirs in disagreements(manifest, segment, _SHARED_FIELDS)
    ]
    if disagree:
        raise EngineError(
            "period manifest disagrees with the journal's segment record"
            f" ({'; '.join(disagree)}): this manifest is not this segment's"
            " (period-model ss2.1)"
        )


# ------------------------------------------------------------- taking a root


@dataclass(frozen=True)
class OpenedRoot:
    """What taking possession of a root yields: the owner and the sentinel."""

    estate_id: str
    sentinel: Sentinel
    resumed: bool


def check_root_unused(run_root: Path) -> None:
    """ss1.1's unused-root predicate over the whole root, not the sentinel alone.

    A root keeping a WAL, a committed period or a populated runs/ is somebody's work even
    without its journal.jsonl. What the launcher pre-stages, `catalogs/`, is excluded.

    It is read-only so the CLI can prove a root claimable before staging a bundle into it."""
    leftovers = [WAL_DIR] if (run_root / WAL_DIR).exists() else []
    periods = run_root / "periods"
    if periods.is_dir() and any(periods.iterdir()):
        leftovers.append("periods (committed)")
    runs = run_root / "runs"
    if runs.is_dir() and any(runs.iterdir()):
        leftovers.append("runs (populated)")
    if leftovers:
        raise EngineError(
            f"{run_root}: no sentinel, but {', '.join(leftovers)} exist(s) --"
            " not an unused root; a genesis here would relabel foreign work (PR-01c)"
        )


def claim_root(run_root: Path) -> OpenedRoot:
    """ss1.1's sentinel step, under its ownership rule. The caller holds `leader.lock`, which
    excludes a concurrent opener.

    A root that already holds this binary's sentinel is resumed; a `journal.jsonl` that is not a
    sentinel is refused; an unused root is claimed with a fresh estate id."""
    existing = read_sentinel(run_root)
    occupied = existing is None and sentinel_path(run_root).exists()
    if existing is None and not occupied:
        check_root_unused(run_root)
    action = own_or_refuse(
        exists=existing is not None or occupied,
        ours=existing is not None,
        what=str(sentinel_path(run_root)),
        holder="a file this binary does not recognise" if occupied else "nobody",
    )
    if action == "resume":
        assert existing is not None
        return OpenedRoot(estate_id=existing.estate_id, sentinel=existing, resumed=True)
    sentinel = Sentinel(estate_id=str(uuid.uuid4()))
    write_sentinel(run_root, sentinel)
    return OpenedRoot(estate_id=sentinel.estate_id, sentinel=sentinel, resumed=False)


def open_wal(run_root: Path, segment_no: int) -> Path:
    """Make `wal/` durable and return the segment's path."""
    directory = run_root / WAL_DIR
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fsync_dir(directory)
    return wal_path(run_root, segment_no)


def stage_period(
    run_root: Path,
    parsed: "list[JilFile]",
    catalog: CatalogIR,
    profile: RuntimeProfile,
) -> StagedManifest:
    """Materialize the run's inputs and pin its identity (DL-130; DL-66).

    `catalogs/<source_bundle_hash>/` holds the post-placeholder source, byte-exact (F1),
    beside `sources.json`, addressed by those bytes, so a relaunch reuses it. The engine
    installs the committed `periods/000001/manifest.json` at genesis, because `baseline_id`
    and `first_index` are its to know.

    Original paths are recorded because `catalog_hash` covers `SourceSpan.file`."""
    from dsl41.ast_jil import render_preserve
    from dsl41.runner_ledger import STATE_MACHINE_VERSION

    sources = [SourceFile(path=jf.file, text=render_preserve(jf)) for jf in parsed]
    return stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=profile,
        state_machine_version=STATE_MACHINE_VERSION,
    )


def load_bundle_catalog(
    run_root: Path, source_bundle_hash: str, *, permit_unknown: bool = False
) -> CatalogIR:
    """The catalog loaded from the immutable bundle this root holds: the one loader (DL-137). It
    parses under the original paths `sources.json` records, because the hash covers spans."""
    from dsl41.ast_jil import JilParseError, parse
    from dsl41.ir import LoweringError, lower_catalog

    sources: Sequence[SourceFile] = bundle_sources(run_root, source_bundle_hash)
    try:
        return lower_catalog(
            [parse(source.text, file=source.path) for source in sources],
            permit_unknown=permit_unknown,
        )
    except (JilParseError, LoweringError) as exc:
        raise EngineError(
            f"{run_root}: bundle {source_bundle_hash} does not load ({exc}): a catalog that"
            " cannot be rebuilt from its own bundle cannot be replayed"
        ) from exc


def require_period_one(run_root: Path) -> None:
    """A run root holds period 1 and nothing else (2.0.0). A second segment is a 1.x lineage
    this build cannot carry: it refuses by name before any replay or write. An opening that
    names a seal cannot sit in segment 1: `check_segment_record` refuses it."""
    segments = wal_segments(run_root)
    if segments and segments != [GENESIS_SEGMENT_NO]:
        raise EngineError(
            f"{run_root}: holds WAL segments {segments}; a 2.0.0 run root holds segment 1"
            " only, and a 1.x lineage with sealed periods cannot be resumed or read"
        )
