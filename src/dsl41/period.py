"""Period identity: what a period IS, and the artifacts that say so.

Normative spec: `docs/period-model.md` ss1.1 (estate layout,
`source_bundle_hash`, `catalog_hash` v2), ss2.1 (the `segment` record,
`RuntimeProfile`, the two manifests). Built by DL-130.

A period's semantics are `(catalog_hash, runtime_hash,
state_machine_version)`. This module owns all three identities, the two
manifest models that carry them, and the estate layout they live in. It
holds no boundary machinery: the seal, `wal/`, the sentinel and the anchor
are later units, and every estate here has exactly one journal holding
exactly one segment -- period 1.

**`catalog_hash` v2** is sha256 over the ss3.2 canonical form of
`CatalogIR` with `meta` projected to `{source_files}` only. `tool_version`
and `parsed_at` are diagnostic and leave; spans stay. v1 hashed the whole
model, so a patch release that changed nothing but the version string
changed the hash -- and a live estate would then refuse to resume under it,
which DL-100 already named an outage manufactured by bookkeeping. The
version rides explicitly (`catalog_hash_version`), because a hash whose
recipe is inferred from its own value is not a versioned hash: every gate
that compares hashes compares LIKE FOR LIKE through `catalog_hash_for`.

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
from dsl41.runner_procid import durable_write

#: every stored digest is spelled exactly this way; an address outside the
#: grammar is not an address, and a native period must never open under one
_HASH_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


#: The recipe `catalog_hash` names, carried explicitly on every `segment`
#: and every manifest (ss1.1). A legacy `header` journal pins v1 and keeps
#: being compared under v1 for as long as it lives.
CATALOG_HASH_VERSION: Final[int] = 2

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


def catalog_hash_v1(catalog: CatalogIR) -> str:
    """The LEGACY content hash (bare hexdigest, no prefix): sha256 of
    `model_dump_json()` over the whole model, `meta` included.

    Kept because a journal written before DL-130 pins one, and a gate that
    compared a v1 journal against a v2 recomputation would refuse every
    estate that predates this build. Nothing new is written under it."""
    return hashlib.sha256(catalog.model_dump_json().encode("utf-8")).hexdigest()


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


def catalog_hash_at(version: int, catalog: CatalogIR) -> str:
    """`catalog`'s hash under the recipe `version` names.

    One function rather than a choice at each gate. The two hashes never
    compare equal -- one is prefixed and the other is not -- so a gate that
    picked wrong would refuse every resume, and a gate that guessed from
    the spelling would be reading the answer out of the question.

    A version this binary does not implement is refused BY NAME, on the
    same rule as `artifact_format_version` (PR-08d): recomputing it under
    v1 would report "the estate changed" over a log this build simply
    cannot read, and that refusal tells an operator to abandon a live
    estate."""
    if version == CATALOG_HASH_VERSION:
        return catalog_hash_v2(catalog)
    if version == 1:
        return catalog_hash_v1(catalog)
    raise EngineError(
        f"catalog_hash_version {version}: this binary implements"
        f" 1 and {CATALOG_HASH_VERSION} (period-model ss1.1)"
    )


def catalog_hash_for(record: Mapping[str, Any], catalog: CatalogIR) -> str:
    """The hash to compare `record`'s `catalog_hash` against: the version a
    `segment` PINS, v1 for a legacy `header` -- which pins none, because it
    was written before there was one to pin.

    The pinned version is read strictly: a `segment` missing the field, or
    carrying `"2"`, `true` or `2.7`, is a malformed identity record and
    refuses -- `int()` coercion would let a record that cannot say which
    recipe it means pick one anyway."""
    if record.get("rec") == "header":
        return catalog_hash_v1(catalog)
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
    directory.parent.mkdir(parents=True, exist_ok=True)
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
        _fsync_dir(directory.parent)
        _fsync_dir(directory.parent.parent)  # run_root's entry for catalogs/
    finally:
        os.close(lock_fd)  # closing releases the flock
    return address


def bundle_source_paths(run_root: Path, source_bundle_hash: str) -> list[Path]:
    """The bundle's stored files, in the order `sources.json` records --
    which is the command-line order the hash was taken over.

    VERIFIED before they are handed out: each stored name is a plain
    basename (a `../` in a hand-edited vector would otherwise walk out of
    the bundle), each file's bytes match the recorded sha256, and the
    framing hash over (recorded paths, stored bytes) reproduces the
    address. The directory is immutable by contract; this is what makes a
    reader's trust in that contract checkable rather than assumed."""
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
    return paths


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
    # dropped either would make this root read as legacy-missing at resume
    _fsync_dir(directory.parent)
    _fsync_dir(run_root)
    return path


def check_manifest_self_consistent(manifest: StagedManifest, where: str) -> None:
    """The manifest must agree with itself (PR-22), checked at every write
    and every read: `runtime_hash` must BE the hash of the profile it
    carries (the segment pins only the hash, so a tampered profile beside
    the original hash would pass every shared-field comparison), its
    `artifact_format_version` must be the one this binary writes (a staged
    2 would open an estate whose own resume refuses it at the ingress),
    and its `catalog_hash_version` must be the current recipe -- a native
    manifest under the legacy recipe would re-manufacture the patch-release
    outage v2 exists to end."""
    if manifest.artifact_format_version != ARTIFACT_FORMAT_VERSION:
        raise EngineError(
            f"{where}: artifact_format_version {manifest.artifact_format_version}:"
            f" this binary writes {ARTIFACT_FORMAT_VERSION}"
        )
    if manifest.catalog_hash_version != CATALOG_HASH_VERSION:
        raise EngineError(
            f"{where}: catalog_hash_version {manifest.catalog_hash_version}: a native"
            f" manifest pins {CATALOG_HASH_VERSION} (period-model ss1.1)"
        )
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
    """The committed manifest, or None when this root has none -- a root
    written before DL-130 carries `manifest/` instead, and a MISSING
    artifact degrades where a WRONG one refuses (DL-113 decision 5).

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


def _fsync_dir(path: Path) -> None:
    """A directory entry is a record too: a rename without this is not
    durable across a power loss."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_canonical_file(path: Path) -> dict[str, Any] | None:
    """The document, or None when the file is not there. A file that IS
    there and does not decode raises: absence is a fact, corruption is
    not -- and absent means exactly ENOENT, because an EACCES or EIO is a
    file that exists and cannot be read, and degrading on it would treat a
    broken root as a legacy one."""
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
    catalog_hash_v1: str | None = None,
) -> dict[str, Any]:
    """ss2.1's `segment`, verbatim: the first record of every segment.

    Self-describing on purpose -- a reader that opens any segment knows the
    period, the catalog and the semantics without reading an earlier file.
    `dsl41_version` is deliberately NOT here: it is per-process, it already
    rides on `leader`, and a patch release must not move these bytes.
    `catalog_hash_v1` is null except on an adopted period 1, where it
    carries the legacy header's hash -- a typed field rather than a
    contradiction with a schema that forbids extras."""
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
        "catalog_hash_v1": catalog_hash_v1,
        "clock_domain": manifest.clock_domain,
        "first_index": manifest.first_index,
        # the three the boundary fills and genesis cannot: this segment
        # opens no seal, was reclaimed from nobody and trusts no unaudited
        # predecessor (ss1.3, ss11)
        "opens_from_seal": None,
        "reclaimed": None,
        "trust_unaudited": None,
        "at": at.isoformat(),
    }


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
    # a `segment` is DL-130-native by definition: the legacy recipe belongs
    # to `header` alone -- and the check is int-first, because JSON loads
    # `2.0` as a float that COMPARES equal to 2
    "catalog_hash_version": lambda v: _int(v) and v == CATALOG_HASH_VERSION,
    "source_bundle_hash": _is_hash,
    "runtime_hash": _is_hash,
    "state_machine_version": _int,
    "catalog_hash_v1": lambda v: v is None or isinstance(v, str),
    "clock_domain": _str,
    "first_index": _int,
    "opens_from_seal": lambda v: v is None or isinstance(v, dict),
    "reclaimed": lambda v: v is None or isinstance(v, dict),
    "trust_unaudited": lambda v: v is None or isinstance(v, dict),
    "at": _str,
}


def check_segment_record(record: Mapping[str, Any]) -> None:
    """A `segment` must BE a ss2.1 segment: every field present with its
    exact type. Run where the record is first read (`read_journal`), so no
    later gate meets `state_machine_version: true` and passes it as 1. A
    legacy `header` is not checked -- it predates the schema."""
    if record.get("rec") != "segment":
        return
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


#: The two record kinds a journal may open with: `segment` is current,
#: `header` is what every log written before DL-130 has. Every reader
#: accepts both; only `segment` is written.
OPENING_RECS: Final[tuple[str, ...]] = ("segment", "header")


def is_opening(record: Mapping[str, Any]) -> bool:
    return record.get("rec") in OPENING_RECS


def opening_at(record: Mapping[str, Any]) -> datetime:
    """When the log was opened. `segment` carries `at` -- which on a
    boundary opening IS the seal's cutoff instant, not restart wall time --
    and a legacy `header` carries `started_at`."""
    stamp = record.get("at") if record.get("rec") == "segment" else record.get("started_at")
    if not isinstance(stamp, str):
        raise EngineError(f"opening record carries no timestamp: {record.get('rec')!r}")
    return datetime.fromisoformat(stamp)


#: The fields a committed manifest and its segment record must agree on
#: (PR-22). Every shared field, not a chosen few: a disagreement in any one
#: of them means the manifest is not this segment's.
_SHARED_FIELDS: Final[tuple[str, ...]] = (
    "catalog_hash",
    "catalog_hash_version",
    "source_bundle_hash",
    "runtime_hash",
    "state_machine_version",
    "period_id",
    "baseline_id",
    "clock_domain",
    "segment_no",
    "first_index",
)


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
