"""Period identity: what a period IS, and the artifacts that say so.

Normative spec: `docs/period-model.md` ss1.1 (estate layout, the sentinel,
`source_bundle_hash`, `catalog_hash` v2), ss2.1 (the `segment` record,
`RuntimeProfile`, the two manifests). Built by DL-130, extended by DL-133.

A period's semantics are `(catalog_hash, runtime_hash,
state_machine_version)`. This module owns all three identities, the two
manifest models that carry them, and the estate layout they live in --
which since DL-133 includes the `period_root` sentinel, `wal/`, `seals/`
and the staging and quarantine directories. It owns the layout and NOT the
boundary: the anchor, the cutoff barrier and the seal operation are
`boundary.py`'s, and they address every path through the functions here.
`own_or_refuse` lives here for the same reason -- ss1.1 states ONE
ownership rule for every root and every anchor, and a rule written twice is
a rule two callers will eventually spell differently.

**`catalog_hash` v2** is sha256 over the ss3.2 canonical form of
`CatalogIR` with `meta` projected to `{source_files}` only. `tool_version`
and `parsed_at` are diagnostic and leave; spans stay. v1 hashed the whole
model, so a patch release that changed nothing but the version string
changed the hash -- and a live estate would then refuse to resume under it,
which DL-100 already named an outage manufactured by bookkeeping. v1 is a
RETIRED dialect since DL-138 and is refused by name, never recomputed. The
version rides explicitly (`catalog_hash_version`), because a hash whose
recipe is inferred from its own value is not a versioned hash: every gate
that compares hashes compares LIKE FOR LIKE through `catalog_hash_for`,
and every one of them dispatches the version through
`check_catalog_hash_version`.

**`source_bundle_hash`** addresses the immutable input bundle by BYTES.
The framing is normative: inputs in command-line order, per file
`len(path) || path || len(bytes) || bytes` with both lengths 8-byte
big-endian counts of UTF-8 bytes. Length framing is what stops `["ab",
"c"]` colliding with `["a", "bc"]`; order is included rather than sorted
away, because `catalog_hash` covers `CatalogMeta.source_files` and the
spans, so reversing two files IS a different catalog and a bundle address
that ignored order would map one directory to two catalog hashes.

**`runtime_hash`** covers the launch options that change interpretation or
dispatch, as a typed frozen model rather than an open list -- so a field
added later is hashed by construction, not by remembering to name it.
Identical JIL launched `--timezone UTC` and then `--timezone
Europe/Zurich` has one `catalog_hash` and two sets of UTC ticks; without
`runtime_hash` classification would report nothing changed.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dsl41.canon import (
    ARTIFACT_FORMAT_VERSION,
    CanonError,
    canonical_bytes,
    decode,
    hash_over,
)
from dsl41.ir import CatalogIR
from dsl41.runner_clock import EngineError
from dsl41.runner_procid import durable_write, fsync_dir, mkdir_durable

#: every stored digest is spelled exactly this way; an address outside the
#: grammar is not an address, and a native period must never open under one
_HASH_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")


def is_hash_address(value: object) -> bool:
    """Whether `value` is a spelled sha256 address -- the one grammar every
    stored digest uses. Public: the seal artifact holds its own address
    fields to the same rule (DL-132)."""
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


_is_hash = is_hash_address  # the schema tables above read the short name


#: The recipe `catalog_hash` names, carried explicitly on every `segment`
#: and every manifest (ss1.1).
CATALOG_HASH_VERSION: Final[int] = 2

#: Retired `catalog_hash` recipes: the version, and the entry that retired
#: it. APPEND-ONLY (docs/protocol-evolution.md ss6) -- a row is never
#: removed and never re-used, because a root written before the retirement
#: can still arrive on an operator's disk and is owed a refusal that names
#: what it holds.
RETIRED_CATALOG_HASH_VERSIONS: Final[dict[int, str]] = {1: "DL-138"}

#: One estate, one journal, one segment, period 1 -- as far as this unit
#: goes. The seal that makes these numbers move is a later unit; naming
#: them keeps the call sites honest about which number they mean.
GENESIS_PERIOD_ID: Final[int] = 1
GENESIS_SEGMENT_NO: Final[int] = 1
GENESIS_FIRST_INDEX: Final[int] = 1

#: ss2.1 defaults, in seconds as the CLI spells them. The engine's own
#: defaults (`runner_adapters`, `runner_startup`) are the same numbers; a
#: profile that disagreed with the running engine would pin a fiction.
FW_DEFAULT_INTERVAL_S: Final[float] = 60.0
CMD_GRACE_S: Final[float] = 10.0
RECONCILE_SETTLE_S: Final[float] = 5.0
SPAWN_WINDOW_S: Final[float] = 5.0
#: ss9's soft gate. The spec states the field and its bound but no
#: number; 60s is what its own worked example gives the closing period
#: (PR-47e). The gate itself is a later unit -- this is the value it will
#: read.
RETRY_HORIZON_S: Final[float] = 60.0


# --------------------------------------------------------------- catalog hash


def catalog_hash_v2(catalog: CatalogIR) -> str:
    """ss1.1: sha256 over the ss3.2 canonical form of `catalog` with `meta`
    projected to `{source_files}` -- `tool_version` and `parsed_at` are
    diagnostic and leave, spans stay. Spelled `"sha256:..."`."""
    payload = catalog.model_dump(mode="json")
    payload["meta"] = {"source_files": list(catalog.meta.source_files)}
    return hash_over(payload)


def _strip_spans(value: Any) -> Any:
    """Every `SourceSpan`/`CondSpan` field in the IR is named exactly `span`
    (`ir.py`, `conditions.py`), so dropping that key recursively drops all
    position metadata. A job that only MOVED in its file must fingerprint the
    same as before it moved."""
    if isinstance(value, dict):
        return {k: _strip_spans(v) for k, v in value.items() if k != "span"}
    if isinstance(value, list):
        return [_strip_spans(v) for v in value]
    return value


def job_fingerprints(catalog: CatalogIR) -> dict[str, str]:
    """Per-job definition fingerprints -- `catalog_hash_v2`'s technique
    (sha256 over a canonical JSON dump) applied one level down.

    The estate-wide hash exists to gate resume and it is deliberately
    conservative: it moves for ANY change anywhere in the estate. That makes
    it the wrong thing to mark a *job's* series with, and the wrong leaf test
    for a classifier. A release touching twelve jobs of eight hundred moves
    it for all eight hundred, so a break line computed from it fires on every
    job and tells the reader nothing about the one they are looking at, and a
    boundary computed from it would drain every live run in the estate.

    **The limit, stated rather than discovered.** The bundle holds the
    POST-placeholder JIL, so this fingerprints the RESOLVED
    definition. An estate whose placeholders vary per run -- `examples/
    nightbank` bakes the run root into `profile`, `std_out_file` and
    `std_err_file`, so every job's resolved text differs between any two run
    roots -- makes every job look changed, and the break line degrades to
    exactly what `catalog_hash` already said. Where placeholders come from a
    stable site.properties, which is the deployment this is for, it says what
    it claims to. The row carries both hashes so a reader can tell the two
    situations apart.

    What this is NOT is a definition diff. "Changed how, and can the state
    carry across it" is the classification `classify.py` builds a graph for
    (period-model ss10.2, which names this function the leaf test); this
    answers only "did this job's definition move".

    It lived in `runner_history.py` as `_job_fingerprints` until DL-131,
    which needed it in a module a pure analysis pass may import. ss10.2
    still names it there and ss15 carries the amendment."""
    return {
        name: hashlib.sha256(
            json.dumps(_strip_spans(job_ir.model_dump(mode="json")), sort_keys=True).encode("utf-8")
        ).hexdigest()
        for name, job_ir in catalog.jobs.items()
    }


def check_catalog_hash_version(version: int, *, where: str) -> None:
    """The ONE `catalog_hash` version dispatch, three ways (DL-138).

    Current proceeds. A RETIRED recipe refuses by name and cites the entry
    that retired it. Anything else refuses as an unknown. "This used to be
    legal" and "this was never legal" are different facts and get different
    errors (docs/protocol-evolution.md ss6): the first says the root
    predates a retirement, the second that the bytes are corrupt or
    foreign, and an operator acts on each differently.

    FOUR owners ask this question -- `check_segment_version` over a segment,
    `check_manifest_self_consistent` over a manifest at every write and
    every read, `Journal.create` over a new log's manifest, and
    `catalog_hash_at` before it recomputes -- and they ask it HERE. The
    fourth was found by implementing the other three: copies of one question
    are how a tree comes to answer it four ways."""
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

    One function rather than a choice at each gate: a gate that guessed the
    recipe from the spelling would be reading the answer out of the
    question.

    The version is DISPATCHED before anything is recomputed, on the same
    rule as `artifact_format_version` (PR-08d): recomputing an unreadable
    recipe under the only one left would report "the estate changed" over a
    log this build simply cannot read, and that refusal tells an operator to
    abandon a live estate."""
    check_catalog_hash_version(version, where="catalog_hash")
    return catalog_hash_v2(catalog)


def catalog_hash_for(record: Mapping[str, Any], catalog: CatalogIR) -> str:
    """The hash to compare `record`'s `catalog_hash` against: the version
    the `segment` PINS.

    The pinned version is read strictly: a `segment` missing the field, or
    carrying `"2"`, `true` or `2.7`, is a malformed identity record and
    refuses -- `int()` coercion would let a record that cannot say which
    recipe it means pick one anyway."""
    version = record.get("catalog_hash_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise EngineError(
            f"segment carries catalog_hash_version {version!r}: not an integer (period-model ss2.1)"
        )
    return catalog_hash_at(version, catalog)


# -------------------------------------------------------- the source bundle


class SourceFile(BaseModel):
    """One input, as the bundle sees it: the path as GIVEN on the command
    line, and the POST-placeholder text that was parsed (byte-exact, F1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    text: str


def source_bundle_hash(sources: Sequence[SourceFile]) -> str:
    """ss1.1, normatively: inputs in command-line order; per file
    `len(path) || path || len(bytes) || bytes`, both lengths 8-byte
    big-endian counts of UTF-8 bytes; sha256 the concatenation.

    The framing is the point. Without it `["ab", "c"]` and `["a", "bc"]`
    are one bundle. The order is the command line's, never sorted."""
    accumulator = hashlib.sha256()
    for source in sources:
        for blob in (source.path.encode("utf-8"), source.text.encode("utf-8")):
            accumulator.update(len(blob).to_bytes(8, "big"))
            accumulator.update(blob)
    return "sha256:" + accumulator.hexdigest()


#: `source_bundle_hash` under a name no parameter shadows -- for callers
#: whose own parameter is named after the value (bundle_source_paths).
source_bundle_hash_fn = source_bundle_hash

#: The address of the empty bundle -- what an engine started with no staged
#: inputs pins. `dsl41 run` always stages one; a rehearsal, an embedder and
#: the bisimulation harness hold a catalog that came from no command line,
#: and "no bundle" is an honest answer where inventing one is not.
EMPTY_BUNDLE_HASH: Final[str] = source_bundle_hash(())


# ------------------------------------------------------------ runtime profile


class RuntimeProfile(BaseModel):
    """ss2.1: the launch options that change interpretation or dispatch, as
    a typed frozen model.

    Every duration is microseconds, present with its resolved default and
    validated against its own bound -- zero is legal for the two windows
    that are `>= 0` and refused for the intervals that are `> 0`. Nothing
    is null except `deadman_us`, which is null when there is no deadman.

    Deliberately NOT here: `artifact_format_version` (it lives on the
    manifest that carries this profile -- two version fields with no
    equality rule are two authorities) and the role->executor route table
    (`ha-deployment.md` ss4 makes a remap carried state revised under
    epoch/CAS, and a remap is not a re-baseline)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_tz: str = Field(default="UTC", min_length=1)
    tz_aliases: dict[str, str] = {}
    as_machine: tuple[str, ...] = ()

    @field_validator("as_machine", mode="before")
    @classmethod
    def _normalized_machines(cls, value: object) -> object:
        """ss2.1 says the field IS "sorted, de-duplicated" -- so the model
        enforces it, not just the CLI path: two spellings of one machine set
        must be one profile and one hash, whoever constructed it. NO
        coercion: a `7` is refused, not spelled `"7"` -- a wire value of the
        wrong type must fail strict validation, not be laundered by the
        normalizer that runs before it."""
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            if any(not isinstance(item, str) for item in items):
                raise ValueError("as_machine items must be strings")
            return tuple(sorted(set(items)))
        return value

    machine_policy: Literal["strict", "local-eligible"] = "strict"
    execution_mode: Literal["tethered", "detached"] = "tethered"
    deadman_us: Annotated[int, Field(gt=0)] | None = None
    fw_default_interval_us: Annotated[int, Field(gt=0)] = 60_000_000
    cmd_grace_us: Annotated[int, Field(gt=0)] = 10_000_000
    reconcile_settle_us: Annotated[int, Field(ge=0)] = 5_000_000
    spawn_window_us: Annotated[int, Field(ge=0)] = 5_000_000
    retry_horizon_us: Annotated[int, Field(gt=0)] = 60_000_000


def runtime_hash(profile: RuntimeProfile) -> str:
    """sha256 over the ss3.2 canonical form of `profile`, `"sha256:..."`."""
    return hash_over(profile.model_dump(mode="json"))


def to_us(seconds: float) -> int:
    """ss2.1's conversion, exactly: `round(seconds * 1_000_000)`."""
    return round(seconds * 1_000_000)


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
) -> RuntimeProfile:
    """The one CLI -> profile normalization (PR-15a).

    Omitted options resolve to the stated defaults; an absent `--timezone`
    is `UTC`, never null; `--as-machine` is de-duplicated and sorted, so
    two spellings of one machine set are one profile; seconds become
    microseconds by `round`. Nothing here decides a BOUND -- the model
    refuses a value outside its own, at construction."""
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
    )


# --------------------------------------------------------------- the manifests


class StagedManifest(BaseModel):
    """ss2.1: what the LAUNCHER pins about a period -- nothing the engine
    owns. The seal path stages one of these for the period it proposes;
    genesis builds one and installs it in the same breath."""

    model_config = ConfigDict(frozen=True, extra="forbid")

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
        """The engine's half (ss2.1): the staged fields plus the five it
        alone can know. `first_index` is attempt output and `baseline_id`
        is minted at the opening, so neither can be staged."""
        return Manifest(
            **self.model_dump(),
            period_id=period_id,
            baseline_id=baseline_id,
            clock_domain=clock_domain,
            segment_no=segment_no,
            first_index=first_index,
        )


class Manifest(StagedManifest):
    """ss2.1: the committed period manifest -- the staged fields plus the
    engine-derived ones. Subclassing IS the "staged fields plus" the spec
    states; and because both models forbid extras, a committed manifest
    never validates as a staged one, which is what keeps a reader from
    accepting the wrong file."""

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
    """The committed manifest of period 1 -- one estate, one journal, one
    segment, and no seal above it.

    `staged` is what the launcher pinned; a caller with only a catalog -- a
    rehearsal, an embedder, the bisimulation harness -- gets the default
    runtime profile over the empty bundle, because "no bundle was staged"
    is an honest answer where inventing one is not. `baseline_id` is minted
    here unless the caller has one: it is the period's, derived per period
    (DL-117), and genesis is the one opening with no seal to take it
    from."""
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
    """A `StagedManifest` over `catalog` under `profile` -- the identities
    computed once, here, so the manifest and the `segment` record that
    names it can never be computed two different ways."""
    return StagedManifest(
        catalog_hash=catalog_hash_v2(catalog),
        source_bundle_hash=source_bundle_hash,
        runtime_profile=profile,
        runtime_hash=runtime_hash(profile),
        state_machine_version=state_machine_version,
    )


# ------------------------------------------------------------------- layout


def bundle_dir(run_root: Path, source_bundle_hash: str) -> Path:
    """`<root>/catalogs/<digest>/` -- content-addressed BY BYTES (ss1.1).

    The directory is named by the digest's hex half, not by the spelled
    `"sha256:..."` value: a colon in an archived path is read as a remote
    host by `rsync` and `scp`, and an estate root is a thing operators
    archive. One spelling is the value's, one is the path's, and this
    function is the only place the two meet."""
    return run_root / "catalogs" / source_bundle_hash.split(":")[-1]


def period_dir(run_root: Path, period_id: int) -> Path:
    """`<root>/periods/<period_id zero-padded to 6>/` (ss1.1)."""
    return run_root / "periods" / f"{period_id:06d}"


def seal_dir(run_root: Path) -> Path:
    """`<root>/seals/` -- one sidecar per closed period (ss1.1)."""
    return run_root / "seals"


def seal_path(run_root: Path, period_id: int) -> Path:
    """`<root>/seals/<period_id zero-padded to 6>.json` (ss1.1)."""
    return seal_dir(run_root) / f"{period_id:06d}.json"


def attestation_path(run_root: Path, period_id: int) -> Path:
    """`<root>/seals/<period_id zero-padded to 6>.audit.json` -- the
    attestation `audit` writes and `verify` reads (ss1.3, ss11).

    Beside the sidecar it attests, under the same zero-padded name, because
    a physical roll imports the pair together and a reader that holds one
    must be able to name the other without a second index."""
    return seal_dir(run_root) / f"{period_id:06d}.audit.json"


def staging_dir(run_root: Path, stage_digest: str) -> Path:
    """`<root>/periods/.staging/<stage digest>/` (ss7).

    Named by the digest's hex half, on `bundle_dir`'s rule: a colon in an
    archived path is read as a remote host by `rsync` and `scp`."""
    return run_root / "periods" / ".staging" / stage_digest.split(":")[-1]


def quarantine_dir(run_root: Path, stage_digest: str, manifest_digest: str) -> Path:
    """`<root>/periods/.quarantine/<old stage digest>/<sha256 of its
    manifest.json>/` (ss7).

    Two levels, because candidates alternate: S1 -> S2 -> S1 -> S2 must
    quarantine four times without a collision, and quarantining the same
    bytes twice must be idempotent (PR-30d)."""
    return (
        run_root
        / "periods"
        / ".quarantine"
        / stage_digest.split(":")[-1]
        / manifest_digest.split(":")[-1]
    )


# -------------------------------------------------------- the sentinel and wal

#: ss1.1: the permanent one-line sentinel every periodized root carries. The
#: NAME is deliberate: a binary that predates the period model refuses `run`
#: because a `journal.jsonl` exists, and there is no instant at which the
#: file is absent. A native root that sealed and exited would otherwise
#: release `leader.lock` over a directory such a binary reads as UNUSED.
SENTINEL_NAME: Final[str] = "journal.jsonl"

#: ss1.1: `wal/000001.jsonl` is period 1, `000002` is period 2 (I1: one
#: period, one segment, one number).
WAL_DIR: Final[str] = "wal"

#: The sentinel's `see` -- where the records went. One value today; it is a
#: field rather than a constant so a reader FOLLOWS it instead of assuming
#: it, which is what makes the layout the sentinel's to state.
WAL_POINTER: Final[Literal["wal/"]] = "wal/"

_WAL_NAME: Final = re.compile(r"^(\d{6})\.jsonl$")


class Sentinel(BaseModel):
    """ss1.1's `period_root` record: one line, one schema, every periodized
    root.

    It says three things a reader needs before it opens anything: which
    estate owns this directory, where the records are, and -- for a root a
    physical roll created -- which claim first opened it, so "this very
    claim" is a fact the sentinel can prove rather than one inferred from
    `estate_id` alone. An in-place opener takes a new claim every period
    and keeps the claim that created the root, so for an in-place claim the
    sentinel proves only that this estate owns this root, which is what it
    needs to prove."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rec: Literal["period_root"] = "period_root"
    artifact_format_version: int = ARTIFACT_FORMAT_VERSION
    estate_id: str = Field(min_length=1)
    see: Literal["wal/"] = WAL_POINTER
    #: the claim that FIRST opened this root (a physical roll's), or null
    claim_id: str | None = None


def split_run_dir(name: str) -> tuple[str, int] | None:
    """`<job>.<run_number>` -- split at the LAST dot, because a job name
    may hold one and a run number may not. CANONICAL spelling only:
    `b.01` aliases `b.1`, and a parser that accepted it would let a
    directory this estate never wrote answer for a real run's evidence
    (ss11a; DL-137). The ONE parser -- retention and the resume sweep
    both read run directories, and two parsers is how they disagreed."""
    job, dot, tail = name.rpartition(".")
    if not dot or not job or not tail.isdigit():
        return None
    if str(int(tail)) != tail:
        return None
    return (job, int(tail))


def sentinel_path(run_root: Path) -> Path:
    return run_root / SENTINEL_NAME


def read_sentinel(run_root: Path) -> Sentinel | None:
    """The root's sentinel, or None when this root has none.

    Absence is a fact and corruption is not: a file that exists and holds a
    `period_root` record this binary cannot read is an `EngineError` naming
    it, never a degrade to "no sentinel". A `journal.jsonl` that is not a
    `period_root` line is not a sentinel either, and the owner that asked
    -- `claim_root`, `plan_retention` -- is the one that names what it
    found (DL-138)."""
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
    except CanonError:
        return None  # not canonical JSON: whatever it is, it is not a sentinel
    if not isinstance(payload, dict) or payload.get("rec") != "period_root":
        return None  # a first line that is not `period_root` is not a sentinel
    try:
        return Sentinel.model_validate(payload)
    except ValidationError as exc:
        raise EngineError(f"{path}: not a sentinel this binary can read ({exc})") from exc


def write_sentinel(run_root: Path, sentinel: Sentinel) -> Path:
    """The sentinel, ss3.2-canonical, by the liturgy.

    NOT create-only here: the ownership rule (`own_or_refuse`) decides
    whether this call may happen, under `leader.lock`, which is what
    excludes a concurrent opener of the same root. Writing it any other way
    -- a bare `open("x")` -- would pass every process-kill test and let a
    power loss restore the previous pathname (ss11 step 3)."""
    path = sentinel_path(run_root)
    durable_write(str(path), canonical_bytes(sentinel.model_dump(mode="json")) + b"\n")
    fsync_dir(run_root)
    return path


def wal_path(run_root: Path, segment_no: int) -> Path:
    """`<root>/wal/<segment_no zero-padded to 6>.jsonl` (ss1.1)."""
    return run_root / WAL_DIR / f"{segment_no:06d}.jsonl"


def wal_segments(run_root: Path) -> list[int]:
    """Every segment this root holds, in order.

    A file under `wal/` that is not `<six digits>.jsonl` is **foreign** and
    refused rather than ignored: I1 makes the name the period number, and a
    second candidate for one period is exactly what draft 4's size roll
    could not recover from. Dot-files are the liturgy's own temporaries and
    are not candidates."""
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


def closed_periods(run_root: Path) -> list[int]:
    """Every period this root can AUDIT, in order: a committed seal AND the
    segment whose evidence re-derives it.

    A rolled root holds the seal it opened from and none of that period's
    WAL or spool, by design (period-model ss1.3), so the imported seal is
    this root's to `verify` and another root's to audit. Naming it here
    would make `dsl41 audit` on a rolled root refuse work it was never
    asked to do."""
    directory = seal_dir(run_root)
    if not directory.is_dir():
        return []
    return sorted(
        int(entry.stem)
        for entry in directory.glob("*.json")
        if entry.stem.isdigit()
        and not entry.name.endswith(".audit.json")
        and wal_path(run_root, int(entry.stem)).exists()
    )


def attestation_periods(run_root: Path) -> list[int]:
    """Every period this root holds an ATTESTATION for, in order.

    The FILE, not its verdict: `dsl41 verify` is what decides whether the
    newest one proves anything, and a reader that had to verify before it
    could name the candidates could not report one that does not (ss1.3,
    ss11). Beside `closed_periods` because both answer "which periods does
    this root hold an artifact for", and because the artifact's name is
    `attestation_path`'s to spell: after DL-137 no caller outside this
    module builds it."""
    directory = seal_dir(run_root)
    if not directory.is_dir():
        return []
    return sorted(
        int(entry.name.split(".")[0])
        for entry in directory.glob("*.audit.json")
        if entry.name.split(".")[0].isdigit()
    )


def active_wal(run_root: Path) -> Path:
    """The segment an appender opens: the newest this root holds, or
    segment 1 on a root whose genesis has not written one yet."""
    segments = wal_segments(run_root)
    return wal_path(run_root, segments[-1] if segments else GENESIS_SEGMENT_NO)


def resolve_wal(path: Path | str) -> Path:
    """The WAL a caller means by `path` -- **the sentinel's `see`, followed
    once, here** (ss1.1).

    A caller may hold any of three things and mean the same file: an estate
    root, that root's sentinel, or a WAL segment. A periodized root's
    sentinel says where the records went; a root without one has no records
    this binary reads. Every reader in the tree goes through this, so the
    layout is the sentinel's to state and no second module gets an opinion
    about it."""
    path = Path(path)
    if path.is_dir():
        return estate_wal(path)
    if path.name == SENTINEL_NAME and read_sentinel(path.parent) is not None:
        return active_wal(path.parent)
    return path


def estate_wal(run_root: Path) -> Path:
    """The segment this root's appender opens: `wal/<newest>.jsonl` on a
    periodized root, and `journal.jsonl` on a root with no sentinel -- which
    is a root this binary has yet to open, or one whose owner refuses it by
    name (DL-138)."""
    return active_wal(run_root) if read_sentinel(run_root) is not None else sentinel_path(run_root)


def root_of_wal(wal: Path) -> Path:
    """The estate root a WAL file belongs to -- the inverse of
    `estate_wal`, and the only place the layout is read backwards.

    A periodized segment is `<root>/wal/<n>.jsonl` and a sentinel-less
    root's file is `<root>/journal.jsonl`, so the parent's NAME is what
    tells the two apart. `estate_wal` puts the layout behind one function on the way
    down; this is the same rule on the way up, so a caller that holds a
    segment and wants its siblings does not spell the shape inline
    (DL-135)."""
    wal = Path(wal)
    return wal.parent.parent if wal.parent.name == WAL_DIR else wal.parent


def estate_segments(path: Path | str) -> list[Path]:
    """Every segment this root RETAINS, oldest first (ss1.1, I1).

    `resolve_wal` answers "which file does an appender open"; this answers
    "which files does the estate still hold", and a reader that wants the
    whole period-crossing record sequence needs the second."""
    wal = resolve_wal(path)
    root = root_of_wal(wal)
    if wal.parent.name != WAL_DIR:
        return [wal]
    return [wal_path(root, segment_no) for segment_no in wal_segments(root)]


def root_is_unused(run_root: Path) -> bool:
    """Whether this directory holds no estate: nothing at the sentinel's
    name. The one question `run` asks before it stages a period."""
    return not sentinel_path(run_root).exists()


def own_or_refuse(
    *, exists: bool, ours: bool, what: str, holder: str
) -> Literal["create", "resume"]:
    """ss1.1's ONE ownership rule, for every root and every anchor:

    > absent -> create. Exact same estate and exact same incomplete
    > transaction -> resume. Anything else -> refuse.

    Written once because it is one rule. "Fresh root" was not a checkable
    rule: E1 could take the free `leader.lock` of a dormant estate E2's
    root R, overwrite R's sentinel and install its imports while E2's
    anchor still named R (PR-01c); and an anchor that merely existed was an
    existing estate whose detached work may still be alive, whatever its
    incumbent's liveness said (PR-01b). The CALLER decides what "ours"
    means, because only it knows which incomplete transaction it is
    resuming."""
    if not exists:
        return "create"
    if ours:
        return "resume"
    raise EngineError(
        f"{what} already exists and is not ours ({holder}): absent creates,"
        " our own incomplete transaction resumes, anything else refuses"
        " (period-model ss1.1)"
    )


def write_bundle(run_root: Path, sources: Sequence[SourceFile]) -> str:
    """Materialize the immutable input bundle; return its address.

    Content-addressed, so this is idempotent by construction: a COMPLETE
    directory holds the same bytes by definition and is REUSED, never
    rewritten -- a period that reverts to earlier inputs references the
    directory already there. Each file is the post-placeholder JIL,
    byte-exact (F1), beside `sources.json` -- the ordered vector of the
    original paths and their sha256, which is what a reopening reads.

    Complete means `sources.json`, not the directory: it is written last,
    so a crash between the `mkdir` and it leaves a directory whose address
    promises bytes it does not hold, and "the directory exists" would
    then reuse that forever. The bundle is assembled in a temp directory
    beside its destination and renamed in, so what appears at the address
    is complete when it appears -- and a concurrent writer that got there
    first is not an error, because the bytes are the name."""
    address = source_bundle_hash(sources)
    directory = bundle_dir(run_root, address)
    # this may be the FIRST write into a fresh run root (the CLI stages
    # before genesis), so the whole chain of created directories -- the
    # run root included -- is made durable here, not left to genesis
    mkdir_durable(str(directory.parent))
    os.chmod(directory.parent, 0o700)
    # EVERYTHING -- the completeness check included -- runs under
    # catalogs/.lock, and the publisher fsyncs before it releases. Three
    # facts have to hold at once and none survives a lock-free step: a
    # crashed writer's incomplete leftover must be repairable (or genesis
    # wedges its own root); a COMPLETE bundle must never be deleted ("check
    # then rmtree" had a window in which completeness arrived between the
    # two); and "complete" must mean DURABLE -- a lock-free fast path could
    # observe a rename whose directory entry was not yet fsynced, reference
    # the bundle durably, and lose it to a power cut.
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
    """The bundle's STORED files, in the order `sources.json` records --
    which is the command-line order the hash was taken over. Verified by
    `_bundle_entries`."""
    return [path for path, _ in _bundle_entries(run_root, source_bundle_hash)]


def bundle_sources(run_root: Path, source_bundle_hash: str) -> list[SourceFile]:
    """The bundle's inputs as the bundle SAW them: the ORIGINAL path each
    was given under, and its post-placeholder text.

    This is what a boundary re-parses (DL-133). `catalog_hash` v2 covers
    spans, and a span names the file it was parsed from, so parsing the
    stored copies under their stored names produces a catalog that can
    never hash back to the staged pin -- the reason `load_catalog_from_manifest`
    is explicitly not hash-gated. Parsed under the ORIGINAL names the
    vector records, the same bytes reproduce the same hash, which is what
    lets an engine validate exactly the staged bytes on a host where the
    original files do not exist (ss7 phase 1)."""
    return [source for _, source in _bundle_entries(run_root, source_bundle_hash)]


def _bundle_entries(run_root: Path, source_bundle_hash: str) -> list[tuple[Path, SourceFile]]:
    """Every stored file with the input it stands for, VERIFIED: each
    stored name is a plain basename (a `../` in a hand-edited vector would
    otherwise walk out of the bundle), each file's bytes match the recorded
    sha256, and the framing hash over (recorded paths, stored bytes)
    reproduces the address. The directory is immutable by contract; this is
    what makes a reader's trust in that contract checkable rather than
    assumed."""
    directory = bundle_dir(run_root, source_bundle_hash)
    payload = _read_canonical_file(directory / "sources.json")
    if payload is None:
        raise EngineError(f"{directory}: no readable sources.json (period-model ss1.1)")
    if payload.get("artifact_format_version") != ARTIFACT_FORMAT_VERSION:
        raise EngineError(
            f"{directory}/sources.json carries artifact_format_version"
            f" {payload.get('artifact_format_version')!r}"
        )
    if payload.get("source_bundle_hash") != source_bundle_hash:
        # the vector's own claim must be the address it sits under: a
        # falsified claim is a malformed artifact even when the files
        # happen to verify
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
    """Stored filenames: the input's basename, disambiguated by position
    when two inputs share one. A pure function of (paths, order), so one
    bundle address always names one set of bytes under one set of names."""
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
    """`periods/<N>/manifest.json`, ss3.2-canonical, by the liturgy.

    0700 on the directory and 0600 on the file, on DL-66's rule: the
    manifest names the estate's inputs and its launch options."""
    # re-checked at the WRITE, not only at the read: `tz_aliases` is a dict
    # on a frozen model -- frozen stops attribute assignment, not mutation
    # of the dict itself -- and a profile mutated after its hash was taken
    # would be committed as a false pin that refuses its own resume
    check_manifest_self_consistent(manifest, f"periods/{manifest.period_id:06d}/manifest.json")
    directory = period_dir(run_root, manifest.period_id)
    directory.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(directory.parent, 0o700)
    directory.mkdir(exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / "manifest.json"
    _write_canonical_file(path, manifest.model_dump(mode="json"))
    # the liturgy fsyncs the file and ITS directory; the entries for
    # periods/<N>/ and periods/ are records too, and a power loss that
    # dropped either would make this root read as manifest-less at resume
    fsync_dir(directory.parent)
    fsync_dir(run_root)
    return path


def check_manifest_self_consistent(manifest: StagedManifest, where: str) -> None:
    """The manifest must agree with itself (PR-22), checked at every write
    and every read: `runtime_hash` must BE the hash of the profile it
    carries (the segment pins only the hash, so a tampered profile beside
    the original hash would pass every shared-field comparison), its
    `artifact_format_version` must be the one this binary writes (a staged
    2 would open an estate whose own resume refuses it at the ingress),
    and its `catalog_hash_version` must be the current recipe -- a native
    manifest under a retired recipe would re-manufacture the patch-release
    outage v2 exists to end."""
    if manifest.artifact_format_version != ARTIFACT_FORMAT_VERSION:
        raise EngineError(
            f"{where}: artifact_format_version {manifest.artifact_format_version}:"
            f" this binary writes {ARTIFACT_FORMAT_VERSION}"
        )
    # the FOURTH owner of the D4 question, through the same dispatcher
    # (DL-138): a manifest is where a segment's pin comes from, and a gate
    # here that spelled the refusal its own way would tell an operator
    # holding a retired root something different from the reader that
    # opens it
    check_catalog_hash_version(manifest.catalog_hash_version, where=where)
    for field in ("catalog_hash", "source_bundle_hash", "runtime_hash"):
        if not _is_hash(getattr(manifest, field)):
            raise EngineError(
                f"{where}: {field} {getattr(manifest, field)!r} is not a sha256 address"
            )
    expected = runtime_hash(manifest.runtime_profile)
    if manifest.runtime_hash != expected:
        raise EngineError(
            f"{where}: runtime_hash {manifest.runtime_hash} is not the hash of the"
            f" profile it carries ({expected}) -- the manifest disagrees with itself"
        )


def read_period_manifest(run_root: Path, period_id: int = GENESIS_PERIOD_ID) -> Manifest | None:
    """The committed manifest, or None when this root has none -- pruned,
    or never written -- because a MISSING artifact degrades where a WRONG
    one refuses (DL-113 decision 5). What "none" then MEANS is the caller's
    to name: `runner_history` discriminates the retired `manifest/` layout
    there, and `runner_startup` refuses a segment root that lost its pin.

    Present but unreadable is the WRONG one: refused as an `EngineError`
    naming the file, so a caller that guards a read with the exceptions
    this tree raises does not meet a bare decoder or validator error."""
    path = period_dir(run_root, period_id) / "manifest.json"
    try:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise EngineError(f"{path}: unreadable: {exc}") from exc
        payload = decode(raw)  # the ss3.2 ingress: dup keys, floats, surrogates
        if not isinstance(payload, dict):
            raise EngineError(f"{path}: not a JSON object")
        missing = sorted(set(Manifest.model_fields) - set(payload))
        if missing:
            # a stored pin with a field absent would silently take the
            # model's default -- a defaulted pin is no pin
            raise EngineError(f"{path}: missing {', '.join(missing)}")
        nested = payload.get("runtime_profile")
        if isinstance(nested, dict):
            gone = sorted(set(RuntimeProfile.model_fields) - set(nested))
            if gone:
                # the same rule one level down: a profile field restored from
                # its default would hash back to the recorded runtime_hash
                # and pass every gate while pinning nothing
                raise EngineError(f"{path}: runtime_profile missing {', '.join(gone)}")
        # strict in the JSON sense: `"10000000"` never coerces to an int and
        # `true` never to 1, while the wire's list is a tuple's one JSON form
        manifest = Manifest.model_validate_json(raw, strict=True)
        check_manifest_self_consistent(manifest, str(path))
        return manifest
    except (CanonError, ValidationError) as exc:
        raise EngineError(f"{path}: not a period manifest this binary can read ({exc})") from exc


def _write_canonical_file(path: Path, payload: Mapping[str, Any]) -> None:
    durable_write(str(path), canonical_bytes(payload))


def _read_canonical_file(path: Path) -> dict[str, Any] | None:
    """The document, or None when the file is not there. A file that IS
    there and does not decode raises: absence is a fact, corruption is
    not -- and absent means exactly ENOENT, because an EACCES or EIO is a
    file that exists and cannot be read, and degrading on it would treat a
    broken root as an empty one."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EngineError(f"{path}: unreadable: {exc}") from exc
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
    """ss2.1's `segment`, verbatim: the first record of every segment.

    Self-describing on purpose -- a reader that opens any segment knows the
    period, the catalog and the semantics without reading an earlier file.
    `dsl41_version` is deliberately NOT here: it is per-process, it already
    rides on `leader`, and a patch release must not move these bytes.

    `opens_from_seal` is null on segment 1 and `{period_id, digest}` on
    every later segment -- `seal.open_from_seal` derives it, and `at` on an
    opening segment IS the seal's cutoff instant rather than restart wall
    time, which is half of what makes two openings of one seal
    byte-identical (PR-07)."""
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
        # the boundary's field, and genesis passes none -- it opens no seal
        "opens_from_seal": dict(opens_from_seal) if opens_from_seal is not None else None,
        # the two break-glass paths (ss1.3, ss11): null until an opener
        # takes one, and filled by the opener that did -- `reclaimed`
        # names the claim a `dsl41 estate reclaim --force` moved out of
        # this opener's way, and the actor who claimed to authorize it
        "reclaimed": dict(reclaimed) if reclaimed is not None else None,
        "trust_unaudited": None,
        "at": at.isoformat(),
    }


def _opens_from_seal_ok(value: object) -> bool:
    """ss2.1's `{period_id, digest}`, exactly -- the two fields recovery
    selects the sidecar by. "Any dict" would let a segment name a seal by
    a key nothing reads and pass every gate below it (DL-132)."""
    return (
        isinstance(value, dict)
        and set(value) == {"period_id", "digest"}
        and _int(value["period_id"])
        and _is_hash(value["digest"])
    )


#: the ss2.1 segment schema: required keys and their exact-type checks.
#: `true` must never pass for `1` -- JSON accepts it and `true == 1` in
#: Python, so a lax reader would let a malformed identity record through
#: every gate that compares by value.
_int = lambda v: isinstance(v, int) and not isinstance(v, bool)  # noqa: E731
_str = lambda v: isinstance(v, str)  # noqa: E731
_SEGMENT_SCHEMA: Final[dict[str, Any]] = {
    "segment_no": _int,
    "estate_id": _str,
    "period_id": _int,
    "baseline_id": _str,
    "catalog_hash": _is_hash,
    # TYPE only here -- which recipes are readable is the D4 dispatcher's
    # question and `check_segment_record` asks it below. The check is
    # int-first, because JSON loads `2.0` as a float that COMPARES equal to 2
    "catalog_hash_version": _int,
    "source_bundle_hash": _is_hash,
    "runtime_hash": _is_hash,
    "state_machine_version": _int,
    "clock_domain": _str,
    # >= 1: index 1 is the first index there ever is, and a forged 0 would
    # make the backfill's positional containment stop at this segment and
    # hide every older one (I2)
    "first_index": lambda v: _int(v) and v >= 1,
    "opens_from_seal": lambda v: v is None or _opens_from_seal_ok(v),
    "reclaimed": lambda v: v is None or isinstance(v, dict),
    "trust_unaudited": lambda v: v is None or isinstance(v, dict),
    "at": _str,
}


#: the ss2.1 segment field names, public for readers that must compare a
#: seal's projection against a record field-for-field (DL-136)
SEGMENT_FIELDS = frozenset(_SEGMENT_SCHEMA)


def check_segment_version(record: Mapping[str, Any]) -> None:
    """The opening's `catalog_hash_version`, read exactly and DISPATCHED --
    before any other check reads the record (DL-138, D4).

    This is a separate function because of WHERE it has to run. The schema
    below describes the CURRENT dialect, and every one of its verdicts is
    "this is not a current segment" -- which is true of a retired segment
    too, and unhelpful. A real version-1 opening carried a BARE HEXDIGEST
    `catalog_hash` and a `catalog_hash_v1` field, so the grammar check and
    the unknown-key check both fire on it; whichever fires first tells an
    operator holding a pre-DL-138 root that their bytes are malformed
    rather than that their root predates a retirement. The version verdict
    owns the file's fate, so it is taken first and the schema never gets to
    describe a record it does not govern.

    The read is exact: a missing field, `true`, `"2"` or `2.0` is a record
    that cannot say which recipe it means, and it is malformed rather than
    old. Those two messages are the schema's own, spelled here because this
    runs before the schema does."""
    if "catalog_hash_version" not in record:
        raise EngineError("segment record missing catalog_hash_version (period-model ss2.1)")
    version = record["catalog_hash_version"]
    if not _int(version):
        raise EngineError(
            f"segment record: catalog_hash_version is {version!r} (period-model ss2.1)"
        )
    check_catalog_hash_version(version, where="segment record")


def check_segment_record(record: Mapping[str, Any]) -> None:
    """A `segment` must BE a ss2.1 segment: every field present with its
    exact type. Run where the record is first read (`read_journal`), so no
    later gate meets `state_machine_version: true` and passes it as 1.

    Unconditional since DL-138: the one opening dialect that was exempt --
    the `header` -- is retired, and `read_journal` refuses it by name before
    this runs."""
    # D4 FIRST, always: which recipe this record is written in decides
    # whether this schema describes it at all (`check_segment_version`)
    check_segment_version(record)
    extras = sorted(set(record) - set(_SEGMENT_SCHEMA) - {"rec"})
    if extras:
        # exact means exact: an unknown key is a record this schema does not
        # describe, and "required fields present" alone would bless it
        raise EngineError(f"segment record carries unknown {', '.join(extras)}")
    for key, check in _SEGMENT_SCHEMA.items():
        if key not in record:
            raise EngineError(f"segment record missing {key} (period-model ss2.1)")
        if not check(record[key]):
            raise EngineError(f"segment record: {key} is {record[key]!r} (period-model ss2.1)")
    # the D4 dispatch already ran, at the top: a retired recipe refuses by
    # name there exactly as it does at `Journal.create` and at
    # `catalog_hash_at` (DL-138)
    # I1 first: segment_no IS period_id (ss3.4 derives one from the other,
    # genesis is 1/1), both positive -- a segment 1 that declared period 3
    # would leave a null lineage link over a period that needs one
    if record["segment_no"] < 1 or record["segment_no"] != record["period_id"]:
        raise EngineError(
            f"segment {record['segment_no']} declares period {record['period_id']}:"
            " one period, one segment, same number (period-model ss2.1, I1)"
        )
    # ss2.1: `opens_from_seal` is null on segment 1 and non-null on every
    # later segment -- every later segment opens a period, and a period
    # opens from a seal. A segment that lost the link is one recovery
    # cannot follow back to the sidecar it opened from.
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
            # the lineage LINK, not just its shape: a seal is the boundary
            # between adjacent periods, so a segment opens exactly the seal
            # that closed its predecessor -- any other period is a graft
            raise EngineError(
                f"segment {record['segment_no']}: opens_from_seal names period"
                f" {named!r} but the predecessor is {record['period_id'] - 1}"
                " (period-model ss2.1)"
            )


#: The one record kind a journal may open with. `header` opened every log
#: written before DL-130 and is a retired dialect since DL-138 --
#: `runner_journal.RETIRED_RECS` refuses it by name.
OPENING_RECS: Final[tuple[str, ...]] = ("segment",)


def is_opening(record: Mapping[str, Any]) -> bool:
    return record.get("rec") in OPENING_RECS


def opening_at(record: Mapping[str, Any]) -> datetime:
    """When the segment was opened -- which on a boundary opening IS the
    seal's cutoff instant, not restart wall time."""
    stamp = record.get("at")
    if not isinstance(stamp, str):
        raise EngineError(f"opening record carries no timestamp: {record.get('rec')!r}")
    return datetime.fromisoformat(stamp)


#: The fields a committed manifest and its segment record must agree on
#: (PR-22). Every shared field, not a chosen few: a disagreement in any one
#: of them means the manifest is not this segment's.
_SHARED_FIELDS = tuple(
    sorted(set(Manifest.model_fields) & SEGMENT_FIELDS)
)  # DERIVED (DL-137): a field added to Manifest and the schema is checked by default


def check_manifest_against_segment(manifest: Manifest, segment: Mapping[str, Any]) -> None:
    """PR-22's U4 half: the committed manifest is the engine's own output
    and is checked against the record that names it, at resume.

    Both sides are named in the refusal. "The manifest disagrees" is not
    actionable; which field, and what each said, is."""
    disagreements = [
        f"{field}: manifest {getattr(manifest, field)!r} vs segment {segment.get(field)!r}"
        for field in _SHARED_FIELDS
        if getattr(manifest, field) != segment.get(field)
    ]
    if disagreements:
        raise EngineError(
            "period manifest disagrees with the journal's segment record"
            f" ({'; '.join(disagreements)}): this manifest is not this segment's"
            " (period-model ss2.1)"
        )
