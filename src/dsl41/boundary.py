"""The boundary: the lineage anchor, the genesis transaction, and the seal
operation.

Normative spec: `docs/period-model.md` ss1.1 (the sentinel and the genesis
transaction), ss1.2/ss1.3 (estate identity and the successor fence), ss6
(the cutoff barrier), ss7 (the seal operation and its three pure phases),
ss8 (preconditions), ss9 (retries across a boundary) and ss11 steps 1-4
(resume). Built by DL-133 as U6b. Obligations PR-01a/b/c, PR-02, PR-03,
PR-04, PR-25 through PR-33 in ss13.

`seal.py` is the ARTIFACT and the two pure functions over it. This module
is the OPERATION: what has to be true before a period may close, in what
order the bytes hit the disk, which single writer performs the cutoff, and
who is allowed to open next. It is the unit above `seal.py` and calls
`close_runtime` and `open_from_seal` unchanged.

**The anchor is the fence, not a one-time claim.** A run root is a path and
identifies nothing (ss1). Two roots that both hold a committed seal with no
following segment would both open the next period, allocate the same
indices and run the same `(job, run_number)` twice -- `concurrency-model.md`
ss0's safety property, violated. Exactly one root may succeed a seal, and
the anchor is where that is decided: a four-state head under a lock, moved
by compare-and-swap, with a durable claim file so a crashed claimant's
replacement can prove it is the same claim rather than a second one.
`LeaderLock`'s pattern rather than a bare `O_EXCL`, because that pattern
already solves what `O_EXCL` does not -- replacement and lifetime.

**The head moves after the fact it records, never before.** Genesis writes
its registry row before any segment exists and marks it provisional; a
successor's row is written in the same anchor write as `claimed -> open`,
after its first segment is durable. Nothing registers a successor before
its segment, or a crash leaves an authoritative row for a period that has
no segment (PR-02c).

**The three writes of a seal are an order, and the order is the whole
durability argument** (ss3): the sidecar, then the `seal` record, then the
anchor CAS. Crash between the first two and the sidecar is an orphan that
recovery ignores; crash between the last two and resume performs the CAS.
The record landing means the sidecar is already durable; the head moving
means the record is. **The `seal` append is the point of no return**: once
any of its bytes may have been written the engine fail-stops rather than
reopening a period that would then append records after a seal line.

**`abort_boundary` runs on every non-commit exit before that append**, not
only on a validation failure: an `ENOSPC` on the sidecar left draft 21's
engine frozen behind a freeze it would never lift (PR-28b). A fence loss
inside the interval fail-stops instead, on DL-101's rule.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import uuid

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dsl41.ast_jil import JilFile, JilParseError, parse, render_preserve
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
from dsl41.classify import Baseline, CarriedState, Classification, classify
from dsl41.ir import CatalogIR, LoweringError, lower_catalog
from dsl41.period import (
    CANDIDATE_NAME,
    CATALOG_HASH_VERSION,
    SENTINEL_NAME,
    STAGED_MANIFEST_NAME,
    WAL_DIR,
    ArchiveReceipt,
    Manifest,
    RuntimeProfile,
    Sentinel,
    SourceFile,
    StagedManifest,
    archive_receipt_path,
    bundle_sources,
    catalog_hash_v2,
    check_manifest_self_consistent,
    check_record_fields,
    disagreements,
    is_hash_address,
    own_or_refuse,
    period_dir,
    quarantine_dir,
    read_sentinel,
    runtime_hash,
    seal_dir,
    seal_path,
    sentinel_path,
    stage_manifest,
    staging_dir,
    wal_path,
    write_bundle,
    write_period_manifest,
    write_sentinel,
    SEGMENT_FIELDS,
)
from dsl41.oracle_state import JobRuntime
from dsl41.runner_admission import DecisionIndex, RequestCollision
from dsl41.runner_clock import EngineError
from dsl41.runner_effects import Effect, EffectOutcome, Outbox
from dsl41.runner_hosts import LOCAL_EXECUTOR_ID
from dsl41.runner_journal import Journal, opens_with_rec, read_journal
from dsl41.runner_ledger import STATE_MACHINE_VERSION, LeaderLock, Proof
from dsl41.runner_procid import (
    current_boot_id,
    durable_write,
    fsync_dir,
    fsync_file,
    mkdir_durable,
    proc_start_token,
)
from dsl41.seal import (
    LIVE_STATUS,
    BoundRun,
    BoundaryRequest,
    CommittedNextPeriod,
    Execution,
    ForcedGate,
    FwWatch,
    OpenedRuntime,
    PendingSpawn,
    Seal,
    SealedState,
    StagedNextPeriod,
    close_runtime,
    open_from_seal,
)

#: ss1.1's anchor directory, which is NOT inside any archivable root.
ANCHOR_NAME: Final[str] = "anchor.json"
ANCHOR_LOCK_NAME: Final[str] = "anchor.lock"
CLAIMS_DIR: Final[str] = "claims"

#: ss1.3: "Local filesystem only. `runner_ledger.py` already says the flock
#: fence is not one on NFS; an NFS anchor is refused at startup" (PR-04). A
#: lock that does not exclude is worse than no lock, because everything
#: above it is written as if it did.
NETWORK_FILESYSTEMS: Final[frozenset[str]] = frozenset(
    {
        "9p",
        "afs",
        "afpfs",
        "ceph",
        "cifs",
        "fuse.sshfs",
        "glusterfs",
        "lustre",
        "nfs",
        "nfs4",
        "smb",
        "smbfs",
        "webdav",
    }
)

#: The sealer waits an unbound SPAWN and an unresolved KILL ladder out
#: rather than snapshotting one (ss3.5, ss8, PR-27, PR-33a). Seconds; it is
#: milliseconds in practice and the bound exists so a wedged tier refuses
#: instead of hanging.
QUIESCE_WAIT_S: Final[float] = 30.0


class PeriodSealed(Exception):
    """ss7: the boundary committed; period N+1 is ready to open.

    Deliberately NOT an `EngineError`: it is a successful, terminal outcome
    of the engine loop, and every `except EngineError` in this tree is a
    failure path. `run` turns it into exit code 3 -- distinct from its
    0/1/2, so an init system does not restart-loop a sealed engine
    (PR-30b) -- and step 9 is `dsl41 run --resume` on the same root."""

    def __init__(self, boundary: CommittedBoundary) -> None:
        super().__init__(
            f"sealed period {boundary.seal.period_id} at {boundary.seal.digest};"
            f" period {boundary.seal.next_period.period_id} is ready to open"
        )
        self.boundary = boundary


class BoundaryFailStop(EngineError):
    """The `seal` append did not complete cleanly (ss7).

    An `fsync` error does not prove the line absent or non-durable, and a
    partial append may have left a torn final line. Draft 22 told that case
    to abort and reopen C1, which would append commands, ticks and
    completions AFTER a seal line that then survives a crash -- records
    after a seal, which recovery rightly refuses. So once any seal bytes may
    have been written the engine fail-stops and reports the outcome
    unknown; recovery decides (PR-28d)."""


class SealRequest(BaseModel):
    """ss2.2's v3 `seal` request, as the engine receives it.

    It names an `expect` on nothing, because it is a boundary and not a row
    mutation, and it carries `request_id` like every command -- which is
    what makes a lost response retryable at all.

    `source` has ONE value: a live seal through the control socket and an
    offline seal from the CLI are one kind of boundary, a request carrying
    an id its caller minted. `adopt` was the other and went with the
    estate-adoption path (DL-138); the field stays because audit DERIVES it
    and compares, and a derivation over a one-value domain is still the
    check that catches a rewritten record.

    `strict=True` (DL-170): `bool` is `int`'s subclass, so a lax `epoch=True`
    would coerce to `1` and let a retry match a committed seal under a type
    the original request never carried (DL-151). `runner_control._seal_wire_error`
    already refuses that on the live socket, with pinned wire prose this
    config cannot reproduce -- so the hand-written gate stays as the
    message layer for every field but one (its own docstring says which,
    and why). `cli_estate.py`'s offline retry route builds a `SealRequest`
    directly, past that gate entirely; every argument it passes is already
    statically typed today, so this is defence in depth for that site, not
    the closing of a live hole -- but a future caller need not stay that
    disciplined for the model to still refuse it. `next_period` needs no
    help from this flag: `StagedNextPeriod` carries its own `strict=True`
    (DL-168), because a nested model validates under ITS OWN config
    regardless of what the outer one sets."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    baseline_id: str
    epoch: int
    request_id: str = Field(min_length=1)
    next_period: StagedNextPeriod
    stage_digest: str
    force_seal: bool = False
    claimed_actor: str = ""
    source: Literal["request"] = "request"

    @property
    def fingerprint(self) -> str:
        return seal_fingerprint(
            source=self.source,
            baseline_id=self.baseline_id,
            epoch=self.epoch,
            next_period=self.next_period,
            force_seal=self.force_seal,
            claimed_actor=self.claimed_actor or None,
        )

    @property
    def boundary_request(self) -> BoundaryRequest:
        return BoundaryRequest(
            source=self.source,
            request_id=self.request_id,
            claimed_actor=self.claimed_actor,
            force_seal=self.force_seal,
        )


@dataclass
class EstateHome:
    """What a live engine needs to know about the lineage it leads.

    One object rather than six constructor parameters, because these six
    facts are one fact: which estate this process is period N of. An engine
    with none -- the bisimulation harness, a rehearsal -- has no lineage and
    a seal request over it has nothing to close."""

    run_root: Path
    anchor: EstateAnchor
    estate_id: str
    #: the CLOSING period's committed manifest: its identity, its runtime
    #: profile, and the `retry_horizon_us` ss9's gate is read from
    manifest: Manifest
    #: the seal this period opened from -- `prev_seal_digest` on the next
    prev_seal_digest: str | None = None
    #: ss2.2's retry route: the `seal` record this period opened from, kept
    #: so an exact retry of the boundary that closed C1 is answered from the
    #: committed seal BEFORE the current-baseline check. The generic v3
    #: parser rejects a foreign `baseline_id` before it reads `request_id`,
    #: and such a retry necessarily carries B1 while C2 answers under B2
    prior_seal_record: dict[str, Any] | None = None


def no_crash(_stage: str) -> None:
    """The crash matrix's seam, and nothing else (`runner_supervisor`'s
    `_crash_point`, generalized to a parameter).

    A no-op in production. Every durable step of the boundary calls it with
    a stage name, so ss11a's crash matrix can stop the operation exactly
    between two writes instead of killing a process and hoping it died in
    the window it meant."""


CrashPoint = Callable[[str], None]


# ------------------------------------------------------------- the anchor


class OpenHead(BaseModel):
    """A period is live in `root`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["open"] = "open"
    period_id: int = Field(ge=1)
    root: str


class ClosedHead(BaseModel):
    """It ended; nobody has opened the next.

    `closing_root` is carried because a physical roll's opener needs to
    find the sidecar, the C2 bundle and the period manifest; draft 5 lost
    it the moment the head moved on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["closed"] = "closed"
    period_id: int = Field(ge=1)
    seal_digest: str
    closing_root: str


class ClaimedHead(BaseModel):
    """One root is opening the next.

    Keyed on the CLAIM, never on the claimant: a crashed claimant's
    replacement necessarily has a different pid, and draft 3's
    process-identity key made an ordinary crash recoverable only by
    `--force`, which is the one operation permitted to fork a lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["claimed"] = "claimed"
    claim_id: str
    target_root: str


#: Retired lineage head states: the state, and the entry that retired it.
#: APPEND-ONLY (docs/protocol-evolution.md ss6). `adopting` was the estate
#: adoption path's fourth head, and an anchor carrying one is refused
#: BEFORE parse -- a discriminated union meets an unknown tag with a
#: validator error that names none of this.
RETIRED_HEAD_STATES: Final[dict[str, str]] = {"adopting": "DL-138"}

Head = Annotated[OpenHead | ClosedHead | ClaimedHead, Field(discriminator="state")]


class PeriodRow(BaseModel):
    """ss1.3's archive registry entry: which root holds this period, and
    what has been proved about it.

    `segment_durable` is what makes a row PROVISIONAL. Genesis inserts its
    row before any segment exists, so every cross-period reader ignores a
    row until it reads `true`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: str
    segment_durable: bool = False
    seal_digest: str | None = None
    attested: bool = False


class Reclaimed(BaseModel):
    """One break-glass override, as the anchor records it (ss1.3).

    Loud, durable and attributable: the claim that was moved, the root that
    held it, who said to move it and when. Nothing here is read to DECIDE
    anything -- the decision was the operator's `--force` -- and everything
    here is read to REPORT it, in the anchor and again in the opening
    `segment` of the period that was let through."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    target_root: str
    next_period: int = Field(ge=2)
    claimed_actor: str
    #: ss3.2's spelling: naive UTC with exactly six fractional digits
    at: str


class Anchor(BaseModel):
    """ss1.3's `anchor.json`: the lineage head plus the archive registry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_format_version: int = ARTIFACT_FORMAT_VERSION
    estate_id: str = Field(min_length=1)
    head: Head
    #: keyed by the period number spelled as a string -- JSON has no
    #: integer keys and ss3.2 sorts every object's keys by code point
    periods: dict[str, PeriodRow] = {}
    #: ss1.3's break-glass ledger: every claim `estate reclaim --force`
    #: moved out of a successor's way, in the order it happened. Append-only
    #: and never consumed -- the next opening `segment` COPIES the entry
    #: into its own `reclaimed` field, so the fork is recorded in the
    #: lineage's own log as well as in the fence that permitted it
    reclaimed: list[Reclaimed] = []

    def row(self, period_id: int) -> PeriodRow | None:
        return self.periods.get(str(period_id))

    def with_row(self, period_id: int, row: PeriodRow) -> dict[str, PeriodRow]:
        return {**self.periods, str(period_id): row}


class Claim(BaseModel):
    """ss1.3's `claims/<claim_id>.json`: the durable successor claim.

    `diag` is diagnostics and nothing else. A crashed claimant's
    replacement has a different pid and start time by construction, so
    identity is the claim -- not the process -- and the fields below exist
    to tell an operator who was here, never to decide anything."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_format_version: int = ARTIFACT_FORMAT_VERSION
    claim_id: str
    estate_id: str
    prev_seal_digest: str
    next_period: int = Field(ge=2)
    target_root: str
    #: ss3.2's spelling: naive UTC with exactly six fractional digits. A
    #: local-time stamp on a canonicalized artifact is a value two readers
    #: in two zones disagree about
    claimed_at: str
    diag: dict[str, Any] = {}


def normalized_root(root: Path | str) -> str:
    """ss1.3: `target_root` is the absolute, normalized path, and is
    persisted that way.

    A claimant started with `--run-root ./r` and restarted with `/abs/r`
    must compute the same `claim_id`, or an ordinary restart needs
    break-glass."""
    return os.path.realpath(str(root))


def claim_id_for(*, prev_seal_digest: str, next_period: int, target_root: Path | str) -> str:
    """ss1.3: `sha256(canonical{prev_seal_digest, next_period,
    target_root})`.

    `next_period` is the NUMBER, not the opening's identity: the seal
    digest already binds every field of the opening the claim is for, so a
    second copy of it here would be a second authority for one fact."""
    return hash_over(
        {
            "next_period": next_period,
            "prev_seal_digest": prev_seal_digest,
            "target_root": normalized_root(target_root),
        }
    )


def filesystem_type(path: Path) -> str | None:
    """The filesystem `path` sits on, or None when this platform will not
    say.

    Read from `/proc/mounts` where it exists and from `mount(8)` otherwise;
    the longest mount point that prefixes the real path wins, which is how
    a bind or a nested mount is resolved. None is NOT a refusal: an
    undetectable filesystem is an unknown, and refusing every unknown would
    make the anchor unusable on a platform this cannot read rather than on
    a substrate the fence does not work on."""
    target = Path(os.path.realpath(str(path)))
    mounts: list[tuple[str, str]] = []
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if len(fields) >= 3:
                    mounts.append((fields[1], fields[2]))
        else:
            out = subprocess.run(
                ["/sbin/mount"], capture_output=True, text=True, check=False, timeout=10
            )
            for line in out.stdout.splitlines():
                # `<device> on <mountpoint> (<type>, <flags>...)`
                if " on " not in line or "(" not in line:
                    continue
                point = line.split(" on ", 1)[1].rsplit(" (", 1)[0]
                mounts.append((point, line.rsplit("(", 1)[1].split(",", 1)[0].rstrip(")")))
    except (OSError, subprocess.SubprocessError):
        return None
    best: tuple[str, str] | None = None
    for point, kind in mounts:
        if (target == Path(point) or Path(point) in target.parents) and (
            best is None or len(point) > len(best[0])
        ):
            best = (point, kind)
    return best[1] if best is not None else None


def check_local_filesystem(anchor_dir: Path) -> None:
    """PR-04: an NFS anchor is refused at startup.

    `flock` on NFS is not a fence, and everything above the anchor is
    written as if it were one -- so the refusal is at the door, where an
    operator can move the directory, rather than at the first fork."""
    kind = filesystem_type(anchor_dir)
    if kind is not None and kind.lower() in NETWORK_FILESYSTEMS:
        raise EngineError(
            f"{anchor_dir} is on {kind}: the anchor's flock is not a fence on a network"
            " filesystem, so two roots could both claim one seal -- put the anchor on"
            " local storage (period-model ss1.3, PR-04)"
        )


class EstateAnchor:
    """ss1.3's lineage authority on this substrate: a directory holding
    `anchor.json`, `anchor.lock` and `claims/`.

    The lock is held for the process lifetime of whoever leads the lineage
    -- the engine in live mode, the sealer in offline mode -- and every
    transition below is a read-compare-write UNDER it. The compare is not
    decoration: the lock excludes another process, and the compare excludes
    this one acting on a head it did not read."""

    def __init__(self, anchor_dir: Path) -> None:
        self.dir = Path(anchor_dir)
        self.path = self.dir / ANCHOR_NAME
        self.lock = LeaderLock(self.dir, ANCHOR_LOCK_NAME, of="estate lineage", held_by="process")

    def acquire(self) -> None:
        # same rule as the run root: every entry down to the anchor
        # directory must be durable before anything relies on the lineage
        # half it holds -- and unconditionally, so a retry after a failed
        # fsync repairs it rather than skipping it
        mkdir_durable(str(self.dir))
        os.chmod(self.dir, 0o700)
        check_local_filesystem(self.dir)
        self.lock.acquire()

    def release(self) -> None:
        self.lock.release()

    def check(self) -> None:
        self.lock.check()

    def read(self) -> Anchor | None:
        """The anchor, or None when this directory holds none.

        Absence is a fact; a file that exists and does not parse is not.
        A present-but-unreadable anchor refuses, because "no anchor" is
        what genesis is allowed to create over."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise EngineError(f"{self.path}: unreadable: {exc}") from exc
        try:
            payload = decode(raw)
            if not isinstance(payload, dict):
                raise EngineError(f"{self.path}: not a JSON object")
            require_artifact_version(payload)  # DL-157
            _check_head_state(self.path, payload)
            # DL-168: still LAX -- unlike `_read_artifact`, this does not read
            # `raw` strict-in-the-JSON-sense, so a laundered `head.period_id`
            # (`true` clearing its `ge=1` floor) is not refused. Reported, not
            # fixed (out of that entry's scope); see
            # test_dl168_an_anchor_still_launders_a_nested_int_field.
            return Anchor.model_validate(payload)
        except (CanonError, ValidationError) as exc:
            raise EngineError(f"{self.path}: not an anchor this binary can read ({exc})") from exc

    def write(self, anchor: Anchor) -> None:
        """One head transition, one liturgy write (ss1.3).

        Rename without `fsync(dir)` is not durable: a power loss could keep
        B's first segment and revert the head, and a second target would
        then claim the same seal. `durable_write` fsyncs the file and the
        directory entry both (PR-02c)."""
        self.lock.check()
        durable_write(str(self.path), canonical_bytes(anchor.model_dump(mode="json")) + b"\n")

    def claim_path(self, claim_id: str) -> Path:
        return self.dir / CLAIMS_DIR / f"{claim_id.split(':')[-1]}.json"

    def read_claim(self, claim_id: str) -> Claim | None:
        path = self.claim_path(claim_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise EngineError(f"{path}: unreadable: {exc}") from exc
        try:
            payload = decode(raw)
            if not isinstance(payload, dict):
                raise EngineError(f"{path}: not a JSON object")
            require_artifact_version(payload)  # DL-157
            # DL-168: still LAX, same gap as `EstateAnchor.read` above -- a
            # laundered `next_period` (a numeric string clearing its `ge=2`
            # floor) is not refused. Reported, not fixed; see
            # test_dl168_a_claim_still_launders_its_int_field.
            return Claim.model_validate(payload)
        except (CanonError, ValidationError) as exc:
            raise EngineError(f"{path}: not a claim this binary can read ({exc})") from exc

    def write_claim(self, claim: Claim) -> None:
        self.lock.check()
        directory = self.dir / CLAIMS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        durable_write(
            str(self.claim_path(claim.claim_id)),
            canonical_bytes(claim.model_dump(mode="json")) + b"\n",
        )

    # ------------------------------------------------------ transitions

    def create_open(self, *, estate_id: str, root: Path, period_id: int = 1) -> Anchor:
        """`absent -> open(period_id, root)`: genesis's third step (ss1.1).

        Create-only under ss1.1's ownership rule, with ONE resume
        exception: our own `open(1, this root, this estate_id)` with no
        committed segment, which is genesis interrupted. Once a segment
        exists, ordinary `--resume` owns recovery and this refuses. An
        existing anchor is an existing estate whose detached work may still
        be alive, whatever its incumbent's liveness says (PR-01b)."""
        existing = self.read()
        ours = (
            existing is not None
            and existing.estate_id == estate_id
            and isinstance(existing.head, OpenHead)
            and existing.head.period_id == period_id
            and normalized_root(existing.head.root) == normalized_root(root)
            and not (existing.row(period_id) or PeriodRow(root="")).segment_durable
        )
        action = own_or_refuse(
            exists=existing is not None,
            ours=ours,
            what=str(self.path),
            holder=_holder_of(existing),
        )
        if action == "resume":
            assert existing is not None
            return existing
        anchor = Anchor(
            estate_id=estate_id,
            head=OpenHead(period_id=period_id, root=normalized_root(root)),
            # provisional until the segment lands: every cross-period
            # reader ignores a row until it reads `segment_durable` (PR-02c)
            periods={str(period_id): PeriodRow(root=normalized_root(root))},
        )
        self.write(anchor)
        return anchor

    def attest(
        self, period_id: int, *, estate_id: str, root: Path | str, seal_digest: str
    ) -> Anchor:
        """The `attested` transition, owned by `audit` (ss1.3): one write
        under the lock, after `audit.json` is durable by the liturgy.

        Idempotent, because a re-run of `audit` over a period already
        attested writes the same file and must not then refuse the flag it
        already set. BOUND, because the caller holds SOME unlocked anchor
        and this row is a claim about one estate's one period in one root:
        flipping it on a stranger's anchor would mark a period attested
        whose proof lives in another lineage entirely."""
        anchor = self.require(estate_id)
        row = anchor.row(period_id)
        if row is None:
            raise EngineError(
                f"{self.path}: period {period_id} has no registry row to attest:"
                " the registry is how a cross-period reader finds a period's root,"
                " and an attestation over a period it does not know names nothing"
                " (period-model ss1.3)"
            )
        if normalized_root(row.root) != normalized_root(root):
            raise EngineError(
                f"{self.path}: period {period_id} lives in {row.root}, not {root} --"
                " this attestation was produced in another estate's root"
                " (period-model ss1.3)"
            )
        if row.seal_digest != seal_digest or not row.segment_durable:
            # exact, null included, and DURABLE: a `seal` record can land
            # before the close CAS, and marking that provisional row
            # attested would certify a boundary the lineage has not
            # committed (period-model ss1.3)
            raise EngineError(
                f"{self.path}: period {period_id} has seal {row.seal_digest!r}"
                f" (segment_durable={row.segment_durable}) and the attestation names"
                f" {seal_digest} -- only a committed, durable row is attested"
                " (period-model ss1.3)"
            )
        if row.attested:
            return anchor
        updated = anchor.model_copy(
            update={
                "periods": anchor.with_row(period_id, row.model_copy(update={"attested": True}))
            }
        )
        self.write(updated)
        return updated

    def reclaim(self, *, estate_id: str, claimed_actor: str) -> tuple[Anchor, Reclaimed]:
        """ss1.3's break-glass: move a `claimed` head back to `closed` so
        another root may claim the seal.

        **A stale claim is break-glass, not garbage.** A `claimed` head
        whose target is unreachable cannot be told from one whose target is
        merely paused, so nothing here decides that -- an operator does,
        under `--force`, and this records the decision: the moved claim and
        the actor who claimed to authorize it come back so the next opening
        `segment` carries them in `reclaimed`. It is the one path in this
        module that can fork a lineage.

        The seal the head goes back to is the claim's own
        `prev_seal_digest`, and the closing root is the registry's row for
        the closing period -- both READ, never supplied, so a reclaim
        cannot invent a lineage the anchor never held."""
        anchor = self.require(estate_id)
        head = anchor.head
        if not isinstance(head, ClaimedHead):
            raise EngineError(
                f"{self.path}: the head is {_spell(head)}, not a claim -- there is"
                " nothing to reclaim, and this verb never moves a head that is"
                " doing its job (period-model ss1.3)"
            )
        claim = self.read_claim(head.claim_id)
        if claim is None:
            raise EngineError(
                f"{self.path}: the head names claim {head.claim_id} and"
                f" {self.claim_path(head.claim_id)} is not there -- the claim file is"
                " written before the head moves, so this state is unreachable without"
                " something deleting it (period-model ss1.3)"
            )
        # the claim's BODY must be what its NAME binds: the id is derived
        # from {prev_seal_digest, next_period, target_root}, so a swapped
        # canonical body under the head's filename recomputes to a
        # different id -- and a reclaim that trusted it would rewrite the
        # head to a lineage this claim id never bound (ss1.3)
        derived = claim_id_for(
            prev_seal_digest=claim.prev_seal_digest,
            next_period=claim.next_period,
            target_root=claim.target_root,
        )
        if derived != head.claim_id or claim.claim_id != head.claim_id:
            raise EngineError(
                f"{self.claim_path(head.claim_id)}: body recomputes to {derived} under"
                f" claim_id {claim.claim_id!r} while the head names {head.claim_id} --"
                " a claim whose name does not bind its body reclaims nothing"
                " (period-model ss1.3)"
            )
        if claim.estate_id != estate_id:
            raise EngineError(
                f"{self.claim_path(head.claim_id)}: estate {claim.estate_id} under an"
                f" anchor naming {estate_id} -- a stranger's claim (period-model ss1.3)"
            )
        if normalized_root(claim.target_root) != normalized_root(head.target_root):
            raise EngineError(
                f"{self.claim_path(head.claim_id)}: targets {claim.target_root} while"
                f" the head names {head.target_root} -- the claim and the head disagree"
                " (period-model ss1.3)"
            )
        closing_period = claim.next_period - 1
        row = anchor.row(closing_period)
        if row is None:
            raise EngineError(
                f"{self.path}: period {closing_period} has no registry row -- the"
                " reclaimed head must go back to a `closed` this anchor can name"
                " (period-model ss1.3)"
            )
        if row.seal_digest != claim.prev_seal_digest or not row.segment_durable:
            raise EngineError(
                f"{self.claim_path(head.claim_id)}: prev_seal_digest"
                f" {claim.prev_seal_digest} while the registry closed period"
                f" {closing_period} under {row.seal_digest!r} -- the head would go back"
                " to a seal this lineage never committed (period-model ss1.3)"
            )
        moved = Reclaimed(
            claim_id=head.claim_id,
            target_root=head.target_root,
            next_period=claim.next_period,
            claimed_actor=claimed_actor,
            at=datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="microseconds"),
        )
        updated = anchor.model_copy(
            update={
                "head": ClosedHead(
                    period_id=closing_period,
                    seal_digest=claim.prev_seal_digest,
                    closing_root=row.root,
                ),
                "reclaimed": [*anchor.reclaimed, moved],
            }
        )
        self.write(updated)
        return updated, moved

    def finalize(self, period_id: int) -> Anchor:
        """Period 1's finalize CAS, performed immediately after its segment
        lands (ss1.3) -- a sixth genesis step of its own, with a recovery
        row for the crash between segment and finalize.

        Idempotent: a re-run over an already-durable row writes nothing, so
        resume may perform it without knowing whether genesis did."""
        anchor = self.require()
        row = anchor.row(period_id)
        if row is None:
            raise EngineError(
                f"{self.path}: period {period_id} has no registry row to finalize"
                " (period-model ss1.3)"
            )
        if row.segment_durable:
            return anchor
        updated = anchor.model_copy(
            update={
                "periods": anchor.with_row(
                    period_id, row.model_copy(update={"segment_durable": True})
                )
            }
        )
        self.write(updated)
        return updated

    def close_period(
        self, *, estate_id: str, period_id: int, root: Path, seal_digest: str
    ) -> Anchor:
        """`open -> closed`: the THIRD write of the seal sequence (ss3).

        Idempotent on a head already closed at this digest, because
        recovery performs exactly this CAS when the record landed and the
        head did not (PR-02b)."""
        anchor = self.require(estate_id)
        head = anchor.head
        if isinstance(head, ClosedHead) and head.period_id == period_id:
            if head.seal_digest != seal_digest:
                raise EngineError(
                    f"{self.path}: period {period_id} is already closed at"
                    f" {head.seal_digest} -- this seal says {seal_digest} (period-model ss1.3)"
                )
            return anchor
        if not isinstance(head, OpenHead) or head.period_id != period_id:
            raise EngineError(
                f"{self.path}: cannot close period {period_id}: the head is"
                f" {_spell(head)} (period-model ss1.3)"
            )
        row = anchor.row(period_id) or PeriodRow(root=normalized_root(root))
        updated = anchor.model_copy(
            update={
                "head": ClosedHead(
                    period_id=period_id,
                    seal_digest=seal_digest,
                    closing_root=normalized_root(root),
                ),
                "periods": anchor.with_row(
                    period_id,
                    row.model_copy(update={"seal_digest": seal_digest, "segment_durable": True}),
                ),
            }
        )
        self.write(updated)
        return updated

    def claim_successor(
        self, *, estate_id: str, seal_digest: str, next_period: int, target_root: Path
    ) -> Claim:
        """`closed -> claimed(claim_id, target_root)`, as one CAS (ss1.3).

        **Idempotent on `claim_id`**: the same `(seal, next_period, root)`
        may resume its own claim after a crash, because identity is the
        claim and not the process. A DIFFERENT `claim_id` against the same
        seal refuses, naming the holder -- overriding that is `estate
        reclaim --force`, the one path here that can fork a lineage."""
        anchor = self.require(estate_id)
        claim_id = claim_id_for(
            prev_seal_digest=seal_digest, next_period=next_period, target_root=target_root
        )
        head = anchor.head
        if isinstance(head, ClaimedHead):
            if head.claim_id != claim_id:
                raise EngineError(
                    f"{self.path}: period {next_period} is already claimed by"
                    f" {head.target_root} (claim {head.claim_id}): a second claimant"
                    " forks the lineage -- `dsl41 estate reclaim --force` is the"
                    " break-glass (period-model ss1.3)"
                )
            claim = self.read_claim(claim_id)
            if claim is not None:
                return claim
        elif not (isinstance(head, ClosedHead) and head.seal_digest == seal_digest):
            raise EngineError(
                f"{self.path}: cannot claim the successor of {seal_digest}: the head is"
                f" {_spell(head)} (period-model ss1.3)"
            )
        claim = Claim(
            claim_id=claim_id,
            estate_id=estate_id,
            prev_seal_digest=seal_digest,
            next_period=next_period,
            target_root=normalized_root(target_root),
            claimed_at=datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="microseconds"),
            diag={
                "boot_id": current_boot_id(),
                "pid": os.getpid(),
                "start_time": proc_start_token(os.getpid()),
            },
        )
        # the claim file first: the head may only name a claim that exists
        self.write_claim(claim)
        self.write(
            anchor.model_copy(
                update={
                    "head": ClaimedHead(claim_id=claim_id, target_root=claim.target_root),
                }
            )
        )
        return claim

    def open_claimed(self, *, claim_id: str, period_id: int, root: Path) -> Anchor:
        """`claimed -> open`, performed after the first `segment` record of
        the new period is durable -- and carrying the successor's registry
        row in the SAME write (ss1.3, PR-02c).

        Never before: a crash between an authoritative row and the segment
        it describes leaves estate-wide readers pointed at a period that
        has none."""
        anchor = self.require()
        head = anchor.head
        if isinstance(head, OpenHead) and head.period_id == period_id:
            return anchor  # the crash was after this very write
        if not isinstance(head, ClaimedHead) or head.claim_id != claim_id:
            raise EngineError(
                f"{self.path}: cannot open period {period_id} under claim {claim_id}:"
                f" the head is {_spell(head)} (period-model ss1.3)"
            )
        updated = anchor.model_copy(
            update={
                "head": OpenHead(period_id=period_id, root=normalized_root(root)),
                "periods": anchor.with_row(
                    period_id, PeriodRow(root=normalized_root(root), segment_durable=True)
                ),
            }
        )
        self.write(updated)
        return updated

    def require(self, estate_id: str | None = None) -> Anchor:
        anchor = self.read()
        if anchor is None:
            raise EngineError(
                f"{self.path}: this lineage has no anchor -- a periodized root without"
                " one cannot prove it is the only successor (period-model ss1.3)"
            )
        if estate_id is not None and anchor.estate_id != estate_id:
            raise EngineError(
                f"{self.path}: anchor estate {anchor.estate_id} is not this root's"
                f" {estate_id}: two geneses are two estates (period-model ss1.2)"
            )
        return anchor


def _spell(head: OpenHead | ClosedHead | ClaimedHead) -> str:
    if isinstance(head, OpenHead):
        return f"open(period {head.period_id}, {head.root})"
    if isinstance(head, ClosedHead):
        return f"closed(period {head.period_id}, {head.seal_digest}, {head.closing_root})"
    return f"claimed({head.claim_id}, {head.target_root})"


def _check_head_state(path: Path, payload: Mapping[str, Any]) -> None:
    """A retired head state refuses BEFORE the anchor is parsed (DL-138).

    Pydantic meets an unknown discriminator tag with "input tag 'adopting'
    found using 'state' does not match any of the expected tags", which
    tells an operator holding a pre-DL-138 anchor nothing about what they
    hold or why it stopped being readable."""
    head = payload.get("head")
    state = head.get("state") if isinstance(head, Mapping) else None
    retired = RETIRED_HEAD_STATES.get(state) if isinstance(state, str) else None
    if retired is not None:
        raise EngineError(
            f"{path}: the lineage head state `{state}` is a RETIRED dialect, refused"
            f" by name since {retired} -- the estate-adoption path that wrote it went"
            " with it (docs/protocol-evolution.md ss6, ss8)"
        )


def _holder_of(anchor: Anchor | None) -> str:
    if anchor is None:
        return "nobody"
    return f"estate {anchor.estate_id}, head {_spell(anchor.head)}"


# --------------------------------------------------- the estate-wide walk


@dataclass(frozen=True)
class EstatePeriod:
    """One resolved registry row: which period, which root holds it, and
    what the row already says about it.

    `archived` is the period's ARCHIVE RECEIPT when it has one (ss12,
    DL-144): the inputs were deleted under a named retention class and the
    attestation stands in for them. It is read whether or not the inputs
    are still on disk, because the receipt is IRREVERSIBLE -- restoring
    files beside one does not un-archive a period, and every reader here
    reports the tier the receipt names."""

    period_id: int
    root: Path
    row: PeriodRow
    archived: ArchiveReceipt | None = None


@dataclass(frozen=True)
class EstateWalk:
    """One lineage, root by root, in period order.

    `periods` holds an entry per registry row whose segment is durable.
    `provisional` holds the NUMBERS of the rows that are not: ss1.3 says a
    row is provisional until its period's first segment lands and that
    every cross-period reader ignores it until then, so they are named
    here rather than dropped."""

    anchor_dir: Path
    estate_id: str
    periods: tuple[EstatePeriod, ...]
    provisional: tuple[int, ...]

    def roots(self) -> tuple[Path, ...]:
        """Every root this lineage names, first appearance first.

        Deduplicated, because a root that opened three periods in place is
        one directory, and a reader that folded it once per period would
        report every row three times."""
        seen: dict[Path, None] = {}
        for entry in self.periods:
            seen.setdefault(entry.root, None)
        return tuple(seen)


def is_anchor_dir(path: Path) -> bool:
    """Whether `path` addresses a LINEAGE ANCHOR rather than a run root.

    The one predicate the estate-wide verbs read their argument with: an
    anchor directory holds `anchor.json`, a run root holds
    `journal.jsonl`, and ss1.1 puts the anchor outside every archivable
    root, so the two are never one directory. A directory holding both is
    not something this layout can produce, and `walk_estate` refuses it by
    name rather than guessing which one the caller meant."""
    return (Path(path) / ANCHOR_NAME).is_file()


def registry_rows(anchor: Anchor, *, anchor_dir: Path) -> list[tuple[int, PeriodRow]]:
    """ss1.3's archive registry as `(period, ROW)`, ascending -- the ONE
    read of it (DL-145).

    Two rules, and both REFUSE rather than drop, because every reader of
    this registry answers a question about the WHOLE estate and a dropped
    row makes a smaller estate look complete.

    **Canonical decimal keys only**, the same rule `split_run_dir` reads
    run directories by: `01` aliases `1`, and two keys naming one period
    would put two roots in one place.

    **No holes.** A row is INSERTED when a root first owns a period and
    never removed, so the keys of a registry this binary wrote are 1..max
    with no gap. A gap is an edited anchor.

    Provisional rows are RETURNED, not filtered: whether a row whose
    segment is not yet durable counts is the caller's question (the walk
    names them, the retention plan skips them), and a shared reader that
    answered it would be deciding for both.

    `anchor_dir` names the file in every refusal. It is the caller's
    because the anchor MODEL does not carry its own path, and a refusal
    that could not say which anchor is wrong sends nobody anywhere. The
    lenient second reader this replaced silently dropped a non-canonical
    key and had no hole rule at all, so an edited anchor made the walk
    refuse and the retention plan run over a smaller estate -- one
    registry, two answers, and the deletion side was the lenient one."""
    rows: list[tuple[int, PeriodRow]] = []
    for key, row in anchor.periods.items():
        if not key.isdigit() or str(int(key)) != key:
            raise EngineError(
                f"{anchor_dir}: registry key {key!r} is not a period number --"
                " ss1.3 keys the archive registry by the period spelled as a"
                " decimal, and this row belongs to no period"
            )
        rows.append((int(key), row))
    numbers = {number for number, _ in rows}
    if numbers and numbers != set(range(1, max(numbers) + 1)):
        raise EngineError(
            f"{anchor_dir}: the registry names periods {sorted(numbers)} and a"
            " lineage has no holes -- rows are inserted when a root first owns a"
            " period and never removed (period-model ss1.3)"
        )
    return sorted(rows, key=lambda pair: pair[0])


def walk_estate(anchor_dir: Path) -> EstateWalk:
    """ss1.3's archive registry, read as a lineage: every period this
    estate holds, in period order, with the root that holds it (PR-02f).

    **One walk, four verbs.** `audit`, `journal`, `runs` and `estate
    prune` each gained an estate-wide mode and all four take their roots
    from here. Four private walks would each grow their own idea of what a
    missing root means, and the one that decided "skip it" would report an
    estate smaller than the estate -- the silent loss this project refuses
    everywhere else.

    **A provisional row is ignored and NAMED, never skipped quietly.**
    ss1.3 inserts genesis's row before any segment exists and marks it
    `segment_durable: false`; every cross-period reader ignores it until
    the finalize CAS flips it. That is the spec, so such a row is not a
    refusal -- it is reported in `provisional`, and the verbs say so.

    **Every other row must resolve, or the walk refuses BY NAME.** A root
    that is missing, holds no sentinel, holds one this binary cannot read,
    belongs to another estate, or does not hold the segment of the period
    it is registered for stops the walk, naming which period, which root
    and why. An operator who archived a root away still has the
    single-root verbs; what they may not have is a total that quietly left
    that root out.

    The anchor is READ and never LOCKED, for the reason `plan_retention`
    gives: a live engine holds the lineage lock for its whole process
    lifetime, so a walk that took it could only ever run against a stopped
    estate. The registry is append-mostly and each row is written before
    anything relies on it, so a row this read sees is a row that stays."""
    anchor_dir = Path(anchor_dir)
    if sentinel_path(anchor_dir).is_file():
        if not is_anchor_dir(anchor_dir):
            # the likeliest typo of all: the RUN ROOT named where its
            # lineage's anchor goes. Saying "no anchor here" would be true
            # and useless; the anchor's default place is a fact this can
            # state
            raise EngineError(
                f"{anchor_dir} is a run ROOT, not a lineage anchor: it holds a"
                f" `{SENTINEL_NAME}` sentinel and no `{ANCHOR_NAME}`. Name the"
                f" anchor -- `{default_anchor_dir(anchor_dir)}` unless this"
                " deployment put it elsewhere (period-model ss1.1)"
            )
        raise EngineError(
            f"{anchor_dir} holds both `{ANCHOR_NAME}` and `{SENTINEL_NAME}`: an anchor"
            " is a SIBLING of every root it fences (period-model ss1.1), so this is"
            " not a directory this layout produces and neither reading of it is safe"
            " to guess"
        )
    stored = EstateAnchor(anchor_dir).read()
    if stored is None:
        raise EngineError(
            f"{anchor_dir}: no anchor -- the registry is what says which root holds"
            " which period, and an estate-wide read has no roots without it"
            " (period-model ss1.3)"
        )
    periods: list[EstatePeriod] = []
    provisional: list[int] = []
    for period_id, row in registry_rows(stored, anchor_dir=anchor_dir):
        if not row.segment_durable:
            provisional.append(period_id)
            continue
        root, receipt = _prove_root(stored, period_id, row)
        periods.append(EstatePeriod(period_id=period_id, root=root, row=row, archived=receipt))
    if not periods:
        raise EngineError(
            f"{anchor_dir}: the registry names no period whose segment is durable"
            f" (provisional: {provisional or 'none'}) -- there is no estate to walk"
            " yet (period-model ss1.3)"
        )
    return EstateWalk(
        anchor_dir=anchor_dir,
        estate_id=stored.estate_id,
        periods=tuple(periods),
        provisional=tuple(provisional),
    )


def _prove_root(
    stored: Anchor, period_id: int, row: PeriodRow
) -> tuple[Path, ArchiveReceipt | None]:
    """The registry's claim about one period, checked against the disk.

    Five ways it can be wrong and one refusal each, because "which root
    holds period N" is the only thing the registry is FOR: an operator who
    reads a total has to know it covered every period, and a reader that
    degraded any of these to a skip would answer with a smaller estate and
    no way to tell.

    The sixth answer is the ARCHIVE (ss12, DL-144): a registered period
    whose segment is gone because an archive deleted it, proved by the
    receipt that licensed the deletion. Without a receipt the same disk is
    LOSS and refuses, because accidental loss must never read as
    archiving."""
    # ONE spelling of "the same root" (DL-145): ss1.3 persists `root`
    # absolute and normalized, and every reader that compares or
    # de-duplicates one goes through `normalized_root` -- so `roots()`
    # cannot answer with two names for one directory
    root = Path(normalized_root(row.root))
    where = f"period {period_id}: registry root {root}"
    if not root.is_dir():
        raise EngineError(
            f"{where} is missing -- the registry names it and the estate-wide read"
            " covers every period or none. Name the roots you still have one at a"
            " time instead (period-model ss1.3)"
        )
    try:
        sentinel = read_sentinel(root)
    except EngineError as exc:
        raise EngineError(f"{where} holds a sentinel this binary cannot read: {exc}") from exc
    if sentinel is None:
        raise EngineError(
            f"{where} holds no `{SENTINEL_NAME}` sentinel -- the one file that says a"
            " directory belongs to a lineage (period-model ss1.1)"
        )
    if sentinel.estate_id != stored.estate_id:
        raise EngineError(
            f"{where} belongs to estate {sentinel.estate_id}, and this lineage is"
            f" {stored.estate_id}: two geneses are two estates (period-model ss1.2)"
        )
    # the receipt is proved whether or not the segment survives: DL-144
    # makes the archive IRREVERSIBLE, so a restored input beside a receipt
    # does not move the period back to the derivation-verified tier -- and
    # a receipt this walk cannot prove is not a fact it may report either.
    # `verify_archive_receipt` is the ONE door (attest.py): the sentinel's
    # estate, the sidecar, PR-02e's consumer rule over the attestation,
    # the digest and chain the receipt names, and -- when a file is being
    # excused -- that the list names THAT path
    from dsl41.attest import verify_archive_receipt

    segment = wal_path(root, period_id)
    try:
        receipt = verify_archive_receipt(
            # `licensing` is belt-and-braces here and says so: ss12a's
            # two shapes both name period N's segment, so a receipt for N
            # always licenses THIS file. The argument is what keeps the
            # walk correct on the day that shape rule is relaxed, and the
            # door's own test is what pins it
            root,
            period_id,
            licensing=None if segment.is_file() else segment,
        )
    except EngineError as exc:
        raise EngineError(f"{where}: {exc}") from exc
    if segment.is_file():
        return root, receipt
    if receipt is not None:
        return root, receipt
    raise EngineError(
        f"{where} holds no `{segment.name}` -- the registry says this root holds"
        " this period's segment, and segment N is period N. No archive receipt"
        f" licenses its absence (`{archive_receipt_path(root, period_id).name}`),"
        " so this is LOSS and not an archive: retention writes the receipt before"
        " it deletes anything, exactly so the two can be told apart"
        " (period-model ss1.3, ss12, I1)"
    )


# ------------------------------------------------------- the estate root


@dataclass(frozen=True)
class OpenedRoot:
    """What taking possession of a root yields: who owns it, and where the
    records go."""

    estate_id: str
    sentinel: Sentinel
    resumed: bool


def default_anchor_dir(run_root: Path) -> Path:
    """`<run-root>.anchor`, a SIBLING of the root and never inside it.

    ss1.1 puts the anchor outside every archivable root and names no path
    for it, because the path is a deployment choice. This is the choice a
    caller that made none gets: deterministic from the root, so an operator
    who restarts with the same `--run-root` reaches the same lineage, and
    outside the directory they archive, so `tar`ing the root never carries
    the fence away with it. An explicit `--estate-anchor` overrides it."""
    return run_root.parent / f"{run_root.name}.anchor"


def check_root_unused(run_root: Path) -> None:
    """ss1.1's UNUSED-root predicate: the whole estate surface, not the
    sentinel alone. A root that lost only its journal.jsonl but keeps a
    WAL, a seal, a committed period or a populated runs/ is somebody's
    work, and claiming it would relabel foreign history or run beside
    detached processes still writing into it. What the LAUNCHER
    legitimately pre-stages is excluded: `catalogs/` is content-addressed
    bundle storage and `periods/.staging/` holds candidates -- both are
    written before genesis by design (stage_next_period).

    Read-only, and public for exactly that reason: the CLI proves the root
    is claimable BEFORE it stages a bundle into it or starts a supervisor
    against it -- both are acts on an estate this process may turn out not
    to lead."""
    leftovers = [
        directory.name
        for directory in (run_root / WAL_DIR, seal_dir(run_root))
        if directory.exists()
    ]
    periods = run_root / "periods"
    if periods.is_dir() and any(e.name != ".staging" for e in periods.iterdir()):
        leftovers.append("periods (committed)")
    runs = run_root / "runs"
    if runs.is_dir() and any(runs.iterdir()):
        leftovers.append("runs (populated)")
    if leftovers:
        raise EngineError(
            f"{run_root}: no sentinel, but {', '.join(leftovers)} exist(s) --"
            " not an unused root; a genesis here would relabel foreign work"
            " (period-model ss1.1, PR-01c)"
        )


def claim_root(
    run_root: Path,
    *,
    estate_id: str | None = None,
    claim_id: str | None = None,
) -> OpenedRoot:
    """ss1.1's SENTINEL step, under ss1.1's ownership rule.

    Called with `leader.lock` already held -- the lock is what excludes a
    concurrent opener of the same root, so the check-then-write below has
    no window. Creating the sentinel refuses a target that already holds a
    `journal.jsonl` of any kind -- another estate's, an earlier period of
    this estate's, or a concurrent opener's -- unless it is this estate's
    sentinel for this very claim, left by our own crash (PR-01c).

    A physical roll passes the `claim_id` that first opened the root, so
    "this very claim" is a fact the sentinel proves rather than one
    inferred from `estate_id` alone. An in-place opener passes none and the
    sentinel proves only that this estate owns this root -- which is what
    it needs to prove, because an in-place opener takes a new claim every
    period."""
    existing = read_sentinel(run_root)
    occupied = existing is None and sentinel_path(run_root).exists()
    if occupied:
        # D5, DL-138: this reader does NOT route through `read_journal`, so
        # it carries its own tombstone. A recognised retired OPENING is told
        # apart from unknown residue, because one says the root predates a
        # retirement and the other says the bytes are somebody else's. The
        # tombstone is header-ONLY: `header` was a legal opening once, and a
        # file opening with any other kind -- a retired `result` or `effect`
        # included -- was never a journal, so it falls to the generic
        # refusal below.
        if opens_with_rec(sentinel_path(run_root)) == "header":
            raise EngineError(
                f"{sentinel_path(run_root)}: opens with `header`, a RETIRED record"
                " dialect refused by name since DL-138 -- there is no path from it"
                " into a period lineage, and this root cannot be claimed"
                " (docs/protocol-evolution.md ss6, ss8)"
            )
    if existing is None and not occupied:
        check_root_unused(run_root)
    ours = (
        existing is not None
        and (estate_id is None or existing.estate_id == estate_id)
        and (claim_id is None or existing.claim_id == claim_id)
    )
    action = own_or_refuse(
        exists=existing is not None or occupied,
        ours=ours,
        what=str(sentinel_path(run_root)),
        holder=(
            "a file this binary does not recognise"
            if occupied
            else f"estate {existing.estate_id}, claim {existing.claim_id}"
            if existing is not None
            else "nobody"
        ),
    )
    if action == "resume":
        assert existing is not None
        return OpenedRoot(estate_id=existing.estate_id, sentinel=existing, resumed=True)
    minted = estate_id or str(uuid.uuid4())
    sentinel = Sentinel(estate_id=minted, claim_id=claim_id)
    write_sentinel(run_root, sentinel)
    return OpenedRoot(estate_id=minted, sentinel=sentinel, resumed=False)


def open_wal(run_root: Path, segment_no: int) -> Path:
    """Make `wal/` and hand back the segment's path, with the directory
    entry durable before anything is appended to a file inside it."""
    directory = run_root / WAL_DIR
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fsync_dir(directory)
    return wal_path(run_root, segment_no)


# -------------------------------------------------------------- staging


class Candidate(BaseModel):
    """ss7's `candidate.json`, beside `staged_manifest.json`.

    It exists because the rename to `periods/N+1/` DROPS the digest from
    the path, and a staged-manifest byte comparison cannot stand in for it:
    the candidate identity also binds the request's staged fields one by
    one, and a later retry must be told WHICH differed.

    `strict=True` (DL-168): a future ingress that builds this model from a
    lax `model_validate` still gets the wire's own types on `stage_digest`
    and `artifact_format_version` -- the belt beside `_read_artifact`'s
    braces, not a substitute for them (nested `next_period` validates under
    `StagedNextPeriod`'s own config, which is why that model is strict too)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    artifact_format_version: int = ARTIFACT_FORMAT_VERSION
    stage_digest: str
    next_period: StagedNextPeriod


def stage_period(
    run_root: Path,
    parsed: list[JilFile],
    catalog: CatalogIR,
    profile: RuntimeProfile,
) -> StagedManifest:
    """Materialize period 1's inputs and pin its identity (period-model
    ss1.1, DL-130) -- the self-contained artifact DL-66 asked for, now
    content-addressed.

    `catalogs/<source_bundle_hash>/` holds the POST-PLACEHOLDER source this
    run loaded, byte-exact (render_preserve is F1), beside `sources.json`;
    the directory is addressed by those very bytes, so a relaunch on
    unchanged inputs reuses it and never rewrites it. The engine installs
    the committed `periods/000001/manifest.json` at genesis, because
    `baseline_id` and `first_index` are its to know, not the launcher's.

    The original paths are recorded because `catalog_hash` covers
    `SourceSpan.file`: byte-exact replay against a relocated copy still
    needs them (relocation-independent hashing is a DELIBERATE defer -- it
    orphans every existing journal's resume gate)."""
    sources = [SourceFile(path=jf.file, text=render_preserve(jf)) for jf in parsed]
    return stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=profile,
        state_machine_version=STATE_MACHINE_VERSION,
    )


def staged_next_from(staged_manifest: StagedManifest) -> StagedNextPeriod:
    """`StagedManifest` -> the client-proposal half, DERIVED from the
    model's own fields (DL-137): this projection had four spellings --
    one hand-listed here, one derived in attest, two reflection rebuilds
    in the CLI -- and which fields cross from launcher-pin
    to client-proposal is one fact."""
    return StagedNextPeriod(
        **{name: getattr(staged_manifest, name) for name in StagedNextPeriod.model_fields}
    )


def stage_next_period(
    run_root: Path, *, staged_manifest: StagedManifest, crash_point: CrashPoint = no_crash
) -> StagedNextPeriod:
    """ss7's staging, second step: `staged_manifest.json` and
    `candidate.json` under `periods/.staging/<stage_digest>/`.

    The bundle is the FIRST step and is `period.write_bundle`'s -- two
    sibling destinations cannot be renamed into place at once, and the
    bundle is content-addressed, so a repeat is idempotent and a concurrent
    client writing the same bytes is harmless.

    Nothing here is the engine's. `period_id`, `segment_no`, `baseline_id`,
    `clock_domain` and `first_index` are derived at the boundary and are
    excluded from `stage_digest` by construction (`StagedNextPeriod`), so a
    retry that closes at a different index stages the same identity."""
    check_manifest_self_consistent(staged_manifest, STAGED_MANIFEST_NAME)
    staged = staged_next_from(staged_manifest)
    directory = staging_dir(run_root, staged.stage_digest)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory.parent, 0o700)
    os.chmod(directory, 0o700)
    durable_write(
        str(directory / STAGED_MANIFEST_NAME),
        canonical_bytes(staged_manifest.model_dump(mode="json")) + b"\n",
    )
    crash_point("after_staged_manifest")
    durable_write(
        str(directory / CANDIDATE_NAME),
        canonical_bytes(
            Candidate(stage_digest=staged.stage_digest, next_period=staged).model_dump(mode="json")
        )
        + b"\n",
    )
    fsync_dir(directory)
    fsync_dir(directory.parent)
    crash_point("after_candidate")
    return staged


def staged_bytes_for(
    run_root: Path, stage_digest: str, *, next_period: int
) -> StagedManifest | None:
    """The staged bytes `stage_digest` names, wherever they now live.

    The rename to `periods/N+1/` moves them OUT of `periods/.staging/`, and
    a retry after an install-before-seal crash must still validate exactly
    those bytes -- which is why the install keeps `staged_manifest.json`
    beside `manifest.json` and carries its own `candidate.json` (ss7). The
    installed directory is consulted only when its candidate NAMES this
    digest, so a stale install is never read as this request's staging."""
    manifest = read_staged_manifest(staging_dir(run_root, stage_digest))
    if manifest is not None:
        return manifest
    installed = period_dir(run_root, next_period)
    candidate = read_candidate(installed)
    if candidate is not None and candidate.stage_digest == stage_digest:
        return read_staged_manifest(installed)
    return None


def read_candidate(directory: Path) -> Candidate | None:
    return _read_artifact(directory / CANDIDATE_NAME, Candidate)


def read_staged_manifest(directory: Path) -> StagedManifest | None:
    manifest = _read_artifact(directory / STAGED_MANIFEST_NAME, StagedManifest)
    if manifest is not None:
        check_manifest_self_consistent(manifest, str(directory / STAGED_MANIFEST_NAME))
    return manifest


def _read_artifact(path: Path, model: type[Any]) -> Any:
    """Read and validate one closed artifact, or None when this path holds
    none. A field with a construction default (`artifact_format_version`)
    would otherwise take that default silently on an artifact that never
    carried it (DL-157), so the read side asks the wire to prove the field
    before construction runs. Writers still stamp the default; only the
    read side checks.

    Construction itself parses `raw` a second time, strict in the JSON
    sense (DL-168) -- `model_validate(payload)` on the already-decoded dict
    would coerce a wire `true` into `1` on any int field, and the digest
    computed afterward hashes the coerced value, so nothing catches it. The
    double parse is cheap and has `read_period_manifest`'s
    `Manifest.model_validate_json(raw, strict=True)` as precedent; a
    call-time `strict=True` cascades into every nested field the way a
    model's own `strict=True` config does not.

    `decode(raw)` running FIRST is load-bearing, not incidental ordering:
    pydantic's own JSON parser accepts a duplicate object key (last value
    wins) where `decode` refuses one (PR-12), so `require_artifact_version`
    above always inspects the SAME payload `model_validate_json` goes on to
    validate. Dropping the first parse as "redundant now that pydantic
    parses too" would reopen PR-12 silently."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EngineError(f"{path}: unreadable: {exc}") from exc
    try:
        payload = decode(raw)
        if not isinstance(payload, dict):
            raise EngineError(f"{path}: not a JSON object")
        require_artifact_version(payload)
        return model.model_validate_json(raw, strict=True)
    except (CanonError, ValidationError) as exc:
        raise EngineError(f"{path}: not a {model.__name__} this binary can read ({exc})") from exc


def load_bundle_catalog(
    run_root: Path, source_bundle_hash: str, *, permit_unknown: bool = False
) -> CatalogIR:
    """Any period's catalog, loaded from the immutable bundle this root
    holds -- the ONE loader (DL-137): staging validation and audit both
    parse the same way or `catalog_hash` v2 cannot bind them. Parsed under
    the ORIGINAL paths `sources.json` records, because the hash covers spans
    and a span names its file."""
    sources: Sequence[SourceFile] = bundle_sources(run_root, source_bundle_hash)
    try:
        return lower_catalog(
            [parse(source.text, file=source.path) for source in sources],
            permit_unknown=permit_unknown,
        )
    except (JilParseError, LoweringError) as exc:
        raise EngineError(
            f"{run_root}: bundle {source_bundle_hash} does not load ({exc}):"
            " a catalog that cannot be rebuilt from its own bundle cannot be"
            " validated or audited (period-model ss7)"
        ) from exc


def load_staged_catalog(
    run_root: Path, staged: StagedManifest, *, permit_unknown: bool = False
) -> CatalogIR:
    """C2, loaded from EXACTLY the staged bytes `stage_digest` names (ss7
    phase 1).

    Parsed under the ORIGINAL paths the bundle's `sources.json` records,
    not under the stored copies' names: `catalog_hash` v2 covers spans and
    a span names its file, so parsing the copies would produce a catalog
    that could never hash back to the pin. That is also what lets an engine
    validate a candidate on a host where the original files do not
    exist."""
    return load_bundle_catalog(run_root, staged.source_bundle_hash, permit_unknown=permit_unknown)


# ---------------------------------------------------------- the ss9 gate


def externally_requested_attempts(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every externally requested ATTEMPT in this segment that reached a
    durable decision (ss9).

    "Attempt", not "mutation": the frozen exact-retry promise covers a
    journaled REJECTION and an applied no-op as much as a state change, so
    a `rejected` CAS loser two seconds ago holds the gate exactly as an
    applied `STARTJOB` would. External is `expect is not None` -- the ss6
    mandate is what an external door carries and an engine-side injection
    never does -- and `host` commands are included by the same test."""
    decided = {
        record["index"]
        for record in records
        if record.get("rec") == "decision" and isinstance(record.get("index"), int)
    }
    # an ATTEMPT carries its number under `seq` and a DECISION under `index`
    # -- one is the subscribe cursor and the other is the attempt's own
    # number, and a decision shares its attempt's (DL-89). Reading the wrong
    # key here is silent: the join is empty, so every gate passes and the
    # horizon protects nothing.
    return [
        dict(record)
        for record in records
        if record.get("rec") in ("input", "host")
        and record.get("expect") is not None
        and record.get("seq") in decided
    ]


def retry_horizon_gate(
    records: Sequence[Mapping[str, Any]], *, horizon_us: int, at: datetime, force_seal: bool
) -> ForcedGate | None:
    """ss9's soft gate, and ss3.1's truth table for it.

    Observed age >= horizon, or no externally requested attempt with a
    durable decision in the period (age = infinity) -> the gate passes and
    `forced_gate` is null WHATEVER `force_seal` says, so an unnecessary
    `--force-seal` is recorded in `boundary_request` and engages nothing
    (PR-30). Below the horizon, `force_seal: false` refuses and
    `force_seal: true` commits with the gate's output recorded -- so the
    log alone shows a forced boundary.

    The horizon is read from the CLOSING period's committed manifest by the
    caller, never from an ambient setting: the gate protects retries of
    requests admitted under C1, and the staged C2 profile has no say
    (PR-47e)."""
    attempts = externally_requested_attempts(records)
    if not attempts:
        return None
    last = max(datetime.fromisoformat(str(attempt["at"])) for attempt in attempts)
    observed_age_us = round((at - last).total_seconds() * 1_000_000)
    if observed_age_us >= horizon_us:
        return None
    if not force_seal:
        raise EngineError(
            f"the last externally requested attempt was {observed_age_us / 1_000_000:.3f}s ago"
            f" and the closing period's retry_horizon_us is {horizon_us}: a retry composed"
            " under this baseline can no longer be answered after the boundary."
            " Wait it out, or seal with --force-seal (period-model ss9)"
        )
    return ForcedGate(
        gate="retry_horizon", horizon_us=horizon_us, observed_age_us=max(observed_age_us, 0)
    )


def seal_fingerprint(
    *,
    source: str,
    baseline_id: str,
    epoch: int,
    next_period: StagedNextPeriod,
    force_seal: bool,
    claimed_actor: str | None,
) -> str:
    """ss2.2: the request's fingerprint over the COMPLETE envelope.

    Two requests with one `request_id` and one `next_period` but different
    `force_seal` or actor COLLIDE: force is an authorization and the actor
    is attribution, and neither may be swapped under a retry."""
    return hash_over(
        {
            "baseline_id": baseline_id,
            "claimed_actor": claimed_actor,
            "epoch": epoch,
            "force_seal": force_seal,
            "next_period": next_period.model_dump(mode="json"),
            "source": source,
        }
    )


# ---------------------------------------------------------- the phases


@dataclass(frozen=True)
class StagedContext:
    """Every fact ss7 phase 1 reads, and nothing else.

    "Pure" means exactly that. Draft 17's signatures named two parameters
    for a function that had to read seven things, which invited an
    implementation on filesystem lookups and engine globals that passes
    every functional case and races."""

    staged: StagedNextPeriod
    staged_bytes: StagedManifest
    boundary_request: BoundaryRequest
    request_fingerprint: str
    c1: Baseline
    c2: CatalogIR
    carried_state: CarriedState
    decision_index: DecisionIndex
    state_machine_version: int
    #: the instant preflight's calendar-exhaustion WARN anchors on
    at: datetime


def validate_staged(ctx: StagedContext) -> Classification:
    """ss7 phase 1, at readiness, BEFORE the barrier. No seal, no T.

    A failure here refuses while C1 is still open and correct, which is the
    whole point of running it first: draft 15 let an opener fence a foreign
    root and commit period 1 without ever running C2's readiness, so an
    unsupported artifact version or a failing preflight surfaced only when
    period 2 refused to open -- a committed, unopenable boundary with the
    old engine already fenced."""
    staged, bytes_ = ctx.staged, ctx.staged_bytes
    for name, value in (
        ("the candidate", staged.artifact_format_version),
        ("the staged manifest", bytes_.artifact_format_version),
    ):
        try:
            check_artifact_version({"artifact_format_version": value})
        except CanonError:
            raise EngineError(
                f"{name} carries artifact_format_version {value}: this binary implements"
                f" {ARTIFACT_FORMAT_VERSION} (period-model ss8, PR-08d)"
            ) from None
    disagree = [
        f"{field}: candidate {named!r} vs staged bytes {stored!r}"
        for field, named, stored in disagreements(staged, bytes_, StagedNextPeriod.model_fields)
    ]
    if disagree:
        raise EngineError(
            f"the request names a candidate the staged bytes do not describe"
            f" ({'; '.join(disagree)}): the engine validates exactly the staged"
            " bytes the fingerprint names (period-model ss7)"
        )
    recomputed = catalog_hash_v2(ctx.c2)
    if recomputed != staged.catalog_hash:
        raise EngineError(
            f"the staged bundle hashes to {recomputed} and the candidate pins"
            f" {staged.catalog_hash}: the boundary would open a catalog it did not"
            " validate (period-model ss7 phase 1)"
        )
    profile_hash = runtime_hash(bytes_.runtime_profile)
    if profile_hash != staged.runtime_hash:
        raise EngineError(
            f"the staged runtime profile hashes to {profile_hash} and the candidate pins"
            f" {staged.runtime_hash}: a tampered profile beside the original hash would"
            " pass every shared-field comparison (period-model ss7 phase 1)"
        )
    if staged.state_machine_version != ctx.state_machine_version:
        raise EngineError(
            f"the candidate names state_machine_version {staged.state_machine_version} and"
            f" this period runs v{ctx.state_machine_version}: one executable implements one"
            " version, so an SM bump is a full drain and a new estate, never a transition"
            " (period-model ss2.1, PR-17)"
        )
    errors = _preflight_errors(ctx.c2, bytes_.runtime_profile, at=ctx.at)
    if errors:
        raise EngineError(
            f"the staged estate does not pass preflight ({'; '.join(errors)}):"
            " refuse loudly, run honestly (runner-design ss8)"
        )
    _check_request_id(ctx)
    verdict = classify(
        closing=ctx.c1,
        opening=Baseline(catalog=ctx.c2, profile=bytes_.runtime_profile),
        carried=ctx.carried_state,
    )
    if verdict.refused:
        raise EngineError(
            f"the classification refuses the boundary ({', '.join(verdict.refused)}):"
            " a period never opens over live work whose closure changed"
            " (period-model ss10.1)"
        )
    return verdict


def _preflight_errors(catalog: CatalogIR, profile: RuntimeProfile, *, at: datetime) -> list[str]:
    from dsl41.runner_preflight import preflight

    return [
        f"{item.code}{'' if item.job is None else f' ({item.job})'}"
        for item in preflight(
            catalog,
            machine_policy=profile.machine_policy,
            as_machine=frozenset(profile.as_machine),
            start=at,
            tz_aliases=dict(profile.tz_aliases),
        )
        if item.severity == "ERROR"
    ]


def _check_request_id(ctx: StagedContext) -> None:
    """ss2.2: `request_id` collides across the WHOLE period, not only
    seal-to-seal.

    One `request_id`, one command (`control-protocol.md` ss3) -- so an
    ordinary `STARTJOB` R and a `seal` R cannot both name authoritative
    decisions (PR-30c)."""
    try:
        prior = ctx.decision_index.lookup(ctx.boundary_request.request_id, ctx.request_fingerprint)
    except RequestCollision as exc:
        # the ordinary path: an id already spent on a DIFFERENT command, so
        # the index refuses before it can answer. Re-raised in this rule's
        # own words, because "reuse an id only for an exact retry" does not
        # tell a sealer which of its two ids is the problem
        raise EngineError(
            f"request_id {ctx.boundary_request.request_id} already names another command"
            f" in this period ({exc}): one request_id, one command -- an ordinary"
            " STARTJOB and a seal cannot both name authoritative decisions"
            " (control-protocol ss3, PR-30c)"
        ) from exc
    if prior is not None:
        raise EngineError(
            f"request_id {ctx.boundary_request.request_id} already decided at index"
            f" {prior.index} in this period: a seal is a command like any other"
            " (control-protocol ss3, PR-30c)"
        )


@dataclass(frozen=True)
class BoundaryContext:
    """ss7 phase 2's context: phase 1's facts plus everything the cutoff
    produced.

    `candidate_sidecar` and `candidate_record` are absent, deliberately.
    Phase 2's checks split at the sidecar because its `classification`
    field IS phase 2's output: the classifier has to run BEFORE there is a
    sidecar to put it in. `validate_boundary` is the half that runs first
    and returns the map; `check_candidate` is the half that reads the
    document built from it."""

    staged: StagedContext
    committed: CommittedNextPeriod
    committed_manifest: Manifest
    at: datetime
    post_barrier_state: CarriedState


def validate_boundary(ctx: BoundaryContext) -> Classification:
    """ss7 phase 2's first half, after ss6 step 6 and before the sidecar it
    will be written into.

    The classifier runs AGAIN and **its output is the committed
    classification** -- by construction, because this is where the map the
    sidecar carries comes from. The barrier's own admissions and any
    reconciliation injections may have created executions or latent intent
    phase 1 never saw; an implementation that re-ran only enough to reject
    R and committed the stale phase-1 map would carry a seal whose A
    assumptions omit a latent case the barrier created, and fail audit
    (PR-28a). Deriving it here rather than comparing it afterwards is the
    difference between an enforced rule and a tautology."""
    if not (ctx.at == ctx.post_barrier_state.now):
        raise EngineError(
            f"the carried state is not at the cutoff ({ctx.post_barrier_state.now} vs"
            f" T {ctx.at}): C1 owns every tick <= T (period-model ss6)"
        )
    verdict = classify(
        closing=ctx.staged.c1,
        opening=Baseline(catalog=ctx.staged.c2, profile=ctx.committed_manifest.runtime_profile),
        carried=ctx.post_barrier_state,
    )
    if verdict.refused:
        raise EngineError(
            f"the post-barrier classification refuses the boundary"
            f" ({', '.join(verdict.refused)}): the cutoff's own admissions created live"
            " work whose closure C2 changes (period-model ss7 phase 2, PR-28a)"
        )
    return verdict


def check_candidate(ctx: BoundaryContext, *, sidecar: Seal, record: Mapping[str, Any]) -> None:
    """ss7 phase 2's second half: the checks that read the candidate
    DOCUMENT, over in-memory models.

    A failure here refuses the commit; C1 has advanced and is still open,
    and the caller's `abort_boundary` puts admission back."""
    if ctx.committed.first_index != sidecar.closes_at_index + 1:
        raise EngineError(
            f"the opening's first_index {ctx.committed.first_index} is not"
            f" closes_at_index + 1 ({sidecar.closes_at_index + 1}): I2 makes every index"
            " estate-monotone (period-model ss3.4, PR-05b)"
        )
    check_seal_record(record)
    disagree = _record_disagreements(sidecar, record)
    if disagree:
        raise EngineError(
            f"the `seal` record disagrees with the sidecar it names"
            f" ({'; '.join(disagree)}): recovery selects the sidecar by these"
            " fields and refuses a wrong one (period-model ss2.2)"
        )
    if not (sidecar.state.now == sidecar.scheduler_admitted_through == ctx.at):
        raise EngineError(
            f"the snapshot is not at the cutoff (now {sidecar.state.now.isoformat()},"
            f" admitted through {sidecar.scheduler_admitted_through.isoformat()},"
            f" T {ctx.at.isoformat()}): C1 owns every tick <= T and C2 every tick after"
            " it (period-model ss6)"
        )
    # phase 3's load, over the in-memory candidates: an opening that only
    # validated at resume would commit a boundary nothing can open
    open_from_seal(sidecar, expected_digest=sidecar.digest, manifest=ctx.committed_manifest)


def carried_outbox(opened: OpenedRuntime | None, *, at: datetime) -> Outbox:
    """The intents and applied bindings a period opens HOLDING (ss3.5).

    `outbox_pending` alone is not the whole carry: an APPLIED SPAWN for a
    still-live run is not pending, and an opener that restored only the
    pending half would meet a bound run with no SPAWN in the outbox and
    accept whatever identity the LIST or the spool claimed. Every `bound`
    and `fw_watch` entry reconstructs its effect AND its applied
    resolution, so the preflight's identity gates hold the world to run_id
    A rather than B.

    Built BEFORE the segment's own records are read, never patched in
    afterwards: an `effect_result` in the new segment for an effect born
    in the old one is an outcome the replay has to attach, and
    `Outbox.resolve` refuses an outcome for an effect it never saw --
    which is the right rule and the wrong order."""
    outbox = Outbox()
    if opened is None:
        return outbox
    for effect in opened.outbox_pending:
        outbox.record(effect)
    for entry in opened.executions:
        if entry.kind == "pending_spawn":
            continue  # its pending effect is in outbox_pending already
        effect = Effect(
            effect_id=entry.effect_id,
            kind="SPAWN",
            job=entry.job,
            run_number=entry.run_number,
            executor_id=getattr(entry, "executor_id", LOCAL_EXECUTOR_ID),
            index=entry.index,
            at=at,
            run_id=entry.run_id,
            generation=getattr(entry, "generation", 0),
        )
        outbox.record(effect)
        outbox.resolve(
            EffectOutcome(
                effect_id=entry.effect_id,
                state="applied",
                run_id=entry.run_id,
                detail="carried across the boundary as an applied binding (ss3.5)",
            )
        )
    return outbox


# ------------------------------------------------------- the seal record

#: ss2.2's `seal` record: every field recovery needs to select the sidecar
#: and refuse a wrong one, plus the request half that makes a live seal
#: answerable at all.
_SEAL_SCHEMA: Final[dict[str, Any]] = {
    "estate_id": lambda v: isinstance(v, str) and bool(v),
    "period_id": lambda v: is_wire_int(v) and v >= 1,
    "closes_at_index": lambda v: is_wire_int(v) and v >= 0,
    "at": lambda v: isinstance(v, str),
    "digest": is_hash_address,
    "next_period_id": lambda v: is_wire_int(v) and v >= 2,
    "next_baseline_id": is_hash_address,
    # the CURRENT recipe, not merely a readable one: the `seal` record is a
    # closed artifact and holds to its sidecar's rule, which is why this is
    # not `period.check_catalog_hash_version` (D4, DL-138)
    "catalog_hash_version": lambda v: is_wire_int(v) and v == CATALOG_HASH_VERSION,
    "source": lambda v: v == "request",
    "request_id": lambda v: isinstance(v, str) and bool(v),
    "request_fingerprint": lambda v: isinstance(v, str) and bool(v),
    "claimed_actor": lambda v: isinstance(v, str),
    "force_seal": lambda v: isinstance(v, bool),
}


def seal_record(seal: Seal) -> dict[str, Any]:
    """ss2.2's record, verbatim -- the boundary's DECISION.

    A `seal` request cannot be decided by an ordinary `decision` record:
    before the seal, a crash leaves a durable "applied" for a boundary that
    never happened; after it, records after a seal are forbidden; and not
    at all leaves a lost response unretryable despite the promised
    `request_id`. So the record carries the request identity, and an exact
    retry is answered from the committed seal in the NEW period, ahead of
    the baseline gate."""
    request = seal.boundary_request
    return {
        "rec": "seal",
        "estate_id": seal.estate_id,
        "period_id": seal.period_id,
        "closes_at_index": seal.closes_at_index,
        "at": seal.closed_at.isoformat(),
        "digest": seal.digest,
        "next_period_id": seal.next_period.period_id,
        "next_baseline_id": seal.next_period.baseline_id,
        "catalog_hash_version": seal.catalog_hash_version,
        "source": request.source,
        "request_id": request.request_id,
        "request_fingerprint": seal.request_fingerprint,
        "claimed_actor": request.claimed_actor,
        "force_seal": request.force_seal,
    }


def check_seal_record(record: Mapping[str, Any]) -> None:
    """A `seal` must BE a ss2.2 seal: every field present with its exact
    type, and no field this schema does not describe."""
    if record.get("rec") != "seal":
        raise EngineError(f"not a seal record: rec is {record.get('rec')!r}")
    check_record_fields(record, _SEAL_SCHEMA, where="seal record", cite="period-model ss2.2")


def _record_disagreements(seal: Seal, record: Mapping[str, Any]) -> list[str]:
    """Every field the ss2.2 record duplicates, compared against the
    sidecar it names -- DERIVED (DL-145): the record the seal ITSELF would
    write, against the record on disk, over `_SEAL_SCHEMA`'s own keys.

    So a key added to the schema is compared here for free. What this
    replaced named its fields four ways -- a mirror table, three
    hand-rolled ifs and a four-row tuple -- and every addition to ss2.2 had
    to be remembered twice or be duplicated without ever being checked
    (DL-137's third defect, same class).

    `sidecar X vs record Y` reads off the argument order: the seal's own
    derivation is the left side."""
    return [
        f"{key}: sidecar {mine!r} vs record {theirs!r}"
        for key, mine, theirs in disagreements(seal_record(seal), record, _SEAL_SCHEMA)
    ]


# --------------------------------------------------------- the executions


def executions_at(
    *,
    run_root: Path,
    outbox: Outbox,
    rows: Mapping[str, JobRuntime],
    catalog: CatalogIR,
    interval_default: int,
    watch_prefix: Mapping[tuple[str, int], int] | None = None,
) -> tuple[Execution, ...]:
    """ss3.5's discriminated lifecycle, read off the outbox and the spool.

    `outbox_pending` holds intents not delivered; an APPLIED SPAWN for a
    still-live run is not pending, so a seal that carried only the RUNNING
    row lost `run_id`, `executor_id`, `generation` and the spool binding.
    The three kinds come from three pieces of evidence, and none of them is
    memory: the effect for a `pending_spawn`, `spawn.json` for a `bound`
    run, and the first `watch_seq` lines of `watch.jsonl` for an
    `fw_watch`.

    An applied SPAWN with neither file is the state ss8 forbids -- the
    sealer waits it out (PR-27) -- and reaching here with one is a
    refusal, not a fourth kind."""
    from dsl41.runner_adapters import WATCH_LOG, load_json, read_watch_log

    out: list[Execution] = []
    for effect, state in live_spawns(outbox, rows):
        if state == "pending":
            out.append(
                PendingSpawn(
                    job=effect.job,
                    run_number=effect.run_number,
                    effect_id=effect.effect_id,
                    index=effect.index,
                    run_id=_require_run_id(effect),
                    executor_id=effect.executor_id,
                    generation=effect.generation or 0,
                )
            )
            continue
        if state != "applied":
            continue  # retired or indeterminate: no live execution to carry
        run_dir = run_root / "runs" / f"{effect.job}.{effect.run_number}"
        run_id = _require_run_id(effect)
        if (run_dir / WATCH_LOG).exists():
            # audit passes the per-run positional prefix: a later period's
            # polls in the same file are ITS evidence, and an instant is
            # not a unique log position (ss3.5), so the cut is a count.
            # The count's authority is established by the CALLER
            # (attest.py: the successor segment's `opens_from_seal` pins
            # the sidecar it is read from) -- here a run that has a log but
            # no claimed entry refuses rather than folding a stranger's
            # progress
            bound = (
                None if watch_prefix is None else watch_prefix.get((effect.job, effect.run_number))
            )
            if watch_prefix is not None and bound is None:
                raise EngineError(
                    f"{effect.job}.{effect.run_number}: watch.jsonl exists but the seal"
                    " being re-derived carries no fw_watch entry for this run -- the"
                    " evidence and the claim disagree (period-model ss11)"
                )
            log = read_watch_log(run_dir, prefix=bound)
            assert log is not None  # the file exists and the fold refuses a bad one
            if log.run_id != run_id:
                # DL-118 at the seal: a valid log that appeared since
                # reconciliation and names another run is a stranger's, and
                # a seal that read its progress would relabel that
                # stranger's state as this run's and commit it
                raise EngineError(
                    f"{effect.job}.{effect.run_number}: watch.jsonl names run_id"
                    f" {log.run_id!r} but the bound run is {run_id!r} -- refusing to"
                    " seal a stranger's watch (DL-118)"
                )
            job_ir = catalog.jobs.get(effect.job)
            spec = getattr(job_ir, "exec_", None) if job_ir is not None else None
            interval = getattr(spec, "watch_interval", None) or interval_default
            out.append(
                FwWatch(
                    job=effect.job,
                    run_number=effect.run_number,
                    effect_id=effect.effect_id,
                    index=effect.index,
                    run_id=run_id,
                    watch_seq=log.watch_seq,
                    previous_size=log.size if log.qualifying else None,
                    stable_polls=log.stable_polls,
                    next_poll_at=log.next_poll_at(interval),
                )
            )
            continue
        spawn = load_json(run_dir / "spawn.json")
        if spawn is None:
            raise EngineError(
                f"{effect.job}.{effect.run_number}: an applied SPAWN with no spawn.json"
                " and no watch.jsonl -- ss8 requires every applied CMD SPAWN to be bound"
                " or terminal before the seal commits, and the sealer waits (PR-27)"
            )
        if spawn.get("run_id") != run_id:
            # same DL-118 rule as the watch log: existence is not identity
            raise EngineError(
                f"{effect.job}.{effect.run_number}: spawn.json reports run_id"
                f" {spawn.get('run_id')!r} but the bound run is {run_id!r} -- refusing"
                " to seal a stranger's binding (DL-118)"
            )
        out.append(
            BoundRun(
                job=effect.job,
                run_number=effect.run_number,
                effect_id=effect.effect_id,
                index=effect.index,
                run_id=run_id,
                executor_id=effect.executor_id,
                generation=effect.generation or 0,
                run_dir=str(run_dir.relative_to(run_root)),
            )
        )
    return tuple(sorted(out, key=lambda entry: (entry.index, entry.effect_id)))


def live_spawns(outbox: Outbox, rows: Mapping[str, JobRuntime]) -> list[tuple[Effect, str]]:
    """Every SPAWN effect with a live run behind it, and its outbox state.

    The liveness rule, written once: the effect's run is the row's CURRENT
    run and that row is STARTING or RUNNING. A run that ended has its
    lifecycle in the row, not in an execution entry; a superseded effect
    names a run number the row moved past."""
    out: list[tuple[Effect, str]] = []
    for effect in outbox.effects():
        if effect.kind != "SPAWN":
            continue
        row = rows.get(effect.job)
        if row is None or row.run_number != effect.run_number or row.status not in LIVE_STATUS:
            continue
        state = outbox.state_of(effect.effect_id)
        if state in ("pending", "applied"):
            out.append((effect, state))
    return out


def executing_jobs(outbox: Outbox, rows: Mapping[str, JobRuntime]) -> dict[str, str]:
    """Which jobs have a live execution behind them, from the WAL ALONE --
    `pending` or `applied` (ss10.1's executing tier).

    The classifier asks only this. `pending_spawn`, `bound` and `fw_watch`
    all classify EXECUTING and no ss10 rule tells them apart, so the tier
    needs no spool evidence -- which matters, because readiness runs BEFORE
    the sealer has waited an unbound SPAWN out (ss8, PR-27) and a
    classifier that needed `spawn.json` could not run there at all. The
    SEAL artifact needs more, and `executions_at` reads the spool for it."""
    return {effect.job: state for effect, state in live_spawns(outbox, rows)}


def _require_run_id(effect: Effect) -> str:
    if effect.run_id is None:
        raise EngineError(
            f"{effect.effect_id}: a SPAWN with no run_id -- a run root written before"
            " DL-118 cannot be sealed, because the boundary carries the binding the"
            " effect was supposed to mint (period-model ss2.3, PR-36a)"
        )
    return effect.run_id


# ----------------------------------------------------------- the writes


@dataclass(frozen=True)
class Snapshot:
    """What the cutoff barrier hands the boundary: the estate at T.

    Every field is taken AFTER ss6 step 6 and BEFORE any sidecar byte, in
    the single-writer loop, so nothing here can move while the boundary
    validates it."""

    state: SealedState
    carried: CarriedState
    outbox_pending: tuple[Effect, ...]
    executions: tuple[Execution, ...]
    closes_at_index: int
    at: datetime
    epoch: int


@dataclass(frozen=True)
class CommittedBoundary:
    """A committed boundary: the three artifacts that now name each
    other. `record` is DERIVED (DL-137): it is a pure function of the
    seal, and a stored copy was a field that could go stale plus a
    parameter three construction sites had to fill correctly."""

    seal: Seal
    manifest: Manifest

    @property
    def record(self) -> dict[str, Any]:
        return seal_record(self.seal)


def commit_boundary(
    *,
    run_root: Path,
    anchor: EstateAnchor,
    journal: Journal,
    estate_id: str,
    closing: Manifest,
    staged_ctx: StagedContext,
    staged_manifest: StagedManifest,
    snapshot: Snapshot,
    prev_seal_digest: str | None,
    forced_gate: ForcedGate | None,
    crash_point: CrashPoint = no_crash,
) -> CommittedBoundary:
    """ss6 steps 7-8 and ss3's three writes, in the one order that is the
    durability argument.

    The caller has already frozen admission, drained to T, parked every FW
    task and settled the outbox; everything here is downstream of that and
    runs in the single-writer loop. Every non-commit exit before the `seal`
    append raises, and the CALLER runs `abort_boundary` -- the interval is
    the caller's to reverse, because it is the caller's flag that closed
    admission."""
    staged = staged_ctx.staged
    committed = staged.commit(
        estate_id=estate_id,
        closing_period_id=closing.period_id,
        closes_at_index=snapshot.closes_at_index,
        clock_domain=closing.clock_domain,
    )
    committed_manifest = staged_manifest.commit(
        period_id=committed.period_id,
        baseline_id=committed.baseline_id,
        clock_domain=committed.clock_domain,
        segment_no=committed.segment_no,
        first_index=committed.first_index,
    )
    context = BoundaryContext(
        staged=staged_ctx,
        committed=committed,
        committed_manifest=committed_manifest,
        at=snapshot.at,
        post_barrier_state=snapshot.carried,
    )
    verdict = validate_boundary(context)  # phase 2's classifier run
    sidecar = close_runtime(
        closing=closing,
        estate_id=estate_id,
        epoch=snapshot.epoch,
        prev_seal_digest=prev_seal_digest,
        closes_at_index=snapshot.closes_at_index,
        closed_at=snapshot.at,
        scheduler_admitted_through=snapshot.at,
        state=snapshot.state,
        outbox_pending=snapshot.outbox_pending,
        executions=snapshot.executions,
        classification=verdict,
        staged=staged,
        boundary_request=staged_ctx.boundary_request,
        request_fingerprint=staged_ctx.request_fingerprint,
        forced_gate=forced_gate,
    )
    record = seal_record(sidecar)
    target = period_dir(run_root, committed.period_id)
    # the committed manifest is written into the STAGED directory and the
    # directory is renamed whole, so the artifacts the boundary names are
    # the ones it validated (ss7)
    plan = _prepare_install(
        run_root, staged=staged, committed_manifest=committed_manifest, crash_point=crash_point
    )
    check_candidate(context, sidecar=sidecar, record=record)
    if plan.staging is not None:
        # the quarantine happens HERE and not while the plan was made: the
        # interval up to the `seal` append is reversible, and moving a
        # superseded candidate out of the way is the one destructive act in
        # it. After this point the only remaining exits are the sidecar
        # write and the append, and both leave the estate recoverable.
        if plan.supersedes is not None:
            _quarantine(run_root, target, plan.supersedes)
        os.rename(plan.staging, target)
        fsync_dir(plan.staging.parent)
        fsync_dir(target.parent)
        crash_point("after_install")
    seal_dir(run_root).mkdir(parents=True, exist_ok=True)
    os.chmod(seal_dir(run_root), 0o700)
    fsync_dir(run_root)  # the run root's entry for seals/ is a record too
    durable_write(str(seal_path(run_root, sidecar.period_id)), sidecar.to_bytes())
    crash_point("after_sidecar")
    # ---- the point of no return. Once any of these bytes may have been
    # written, reopening C1 would append commands, ticks and completions
    # AFTER a seal line -- which recovery rightly refuses -- so every
    # failure from here on is a fail-stop with an unknown outcome, never an
    # abort (PR-28d).
    try:
        journal.seal(record)
        crash_point("after_seal_record")
        anchor.close_period(
            estate_id=estate_id,
            period_id=sidecar.period_id,
            root=run_root,
            seal_digest=sidecar.digest,
        )
        crash_point("after_close_cas")
    except Exception as exc:
        raise BoundaryFailStop(
            f"the `seal` append did not complete cleanly ({exc}): the outcome is UNKNOWN"
            f" (request_id {staged_ctx.boundary_request.request_id}). This engine stops"
            " rather than reopening a period that would append records after a seal"
            " line; recovery decides (period-model ss7, PR-28d)"
        ) from exc
    return CommittedBoundary(seal=sidecar, manifest=committed_manifest)


@dataclass(frozen=True)
class _Install:
    """What the install step will do, decided before phase 2 and acted on
    after it: which directory to rename in (None on the reuse path), and
    which installed candidate it supersedes."""

    staging: Path | None
    supersedes: Candidate | None


def _prepare_install(
    run_root: Path,
    *,
    staged: StagedNextPeriod,
    committed_manifest: Manifest,
    crash_point: CrashPoint,
) -> _Install:
    """Put the committed manifest where the boundary will name it, and plan
    the install -- without performing the one destructive act in it (ss7).

    Three cases, and the third is why `candidate.json` exists. The rename
    to `periods/N+1/` drops the digest from the path, so an INSTALLED
    candidate must carry its own identity: a retry whose `stage_digest`
    equals it reuses the staged identity and regenerates `manifest.json`
    from its OWN cutoff -- `first_index` is attempt output, and the first
    attempt's 101 is the retry's 102 -- while a retry with a DIFFERENT one
    quarantines the installed candidate and installs its own, so a stale
    candidate is never silently selected (PR-30d)."""
    target = period_dir(run_root, committed_manifest.period_id)
    installed = read_candidate(target)
    if installed is not None and installed.stage_digest == staged.stage_digest:
        # the reuse path's own liturgy: the fresh path's four fsyncs do not
        # apply to a directory already in place (PR-30d, PR-30g)
        write_period_manifest(run_root, committed_manifest)
        crash_point("after_committed_manifest")
        return _Install(staging=None, supersedes=None)
    if installed is None and target.exists():
        raise EngineError(
            f"{target} exists and carries no candidate.json: the boundary will not"
            " blindly reuse a period directory it cannot identify (period-model ss7)"
        )
    staging = staging_dir(run_root, staged.stage_digest)
    if read_candidate(staging) is None:
        # readiness resolved these bytes, so this is the window between it
        # and the install: a second client, or a retention sweep, removed
        # the staged directory while the barrier ran. Refused, because the
        # alternative is renaming a directory that is not there
        raise EngineError(
            f"{staging}: no staged candidate at this digest -- the request names bytes"
            " that were never staged (period-model ss7)"
        )
    durable_write(
        str(staging / "manifest.json"),
        canonical_bytes(committed_manifest.model_dump(mode="json")) + b"\n",
    )
    crash_point("after_committed_manifest")
    return _Install(staging=staging, supersedes=installed)


def _quarantine(run_root: Path, target: Path, installed: Candidate) -> None:
    """Move a superseded installed candidate out of the way, to a path that
    cannot collide when candidates alternate S1 -> S2 -> S1 and that is
    idempotent when the same bytes are quarantined twice (ss7, PR-30d)."""
    manifest_bytes = (target / "manifest.json").read_bytes()
    destination = quarantine_dir(run_root, installed.stage_digest, hash_over_bytes(manifest_bytes))
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent.parent, 0o700)
    if destination.exists():
        shutil.rmtree(target)  # the same bytes are already quarantined
    else:
        os.rename(target, destination)
    fsync_dir(destination.parent)
    fsync_dir(target.parent)


def hash_over_bytes(data: bytes) -> str:
    """sha256 over bytes already on disk -- the one place a digest is taken
    over stored bytes rather than over a model's canonical form."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class OpenedPeriod:
    """The opening half of a boundary: the new segment and what seeds the
    engine from it."""

    opened: OpenedRuntime
    journal: Journal
    #: None when this root was RE-opening a period whose head had already
    #: moved: the claim was consumed by that move and only the segment was
    #: missing (ss11)
    claim: Claim | None


def open_next_period(
    *,
    run_root: Path,
    anchor: EstateAnchor,
    committed: CommittedBoundary,
    catalog: CatalogIR | Callable[[], CatalogIR],
    lock: Proof | None = None,
    crash_point: CrashPoint = no_crash,
) -> OpenedPeriod:
    """ss7's opening half: claim the successor, write the opening segment,
    move the head, and seed from the seal.

    `at` on the opening segment IS T -- the seal's cutoff instant, not
    restart wall time -- and every non-derived opening field comes from the
    seal's `next_period`, which is what lets two openings of one seal be
    byte-identical (PR-07).

    `catalog` may be a CALLABLE, and then it is resolved AFTER the claim
    and before the segment -- which is exactly where ss7 puts a physical
    roll's import, and the roll cannot load C2 until it has imported the
    bundle. An in-place opener passes the object it already holds and
    nothing happens between the two.

    Idempotent at every step: the claim resumes on its own `claim_id`, an
    already-written segment is verified rather than appended to, and
    `claimed -> open` is a no-op once performed."""
    seal = committed.seal
    opening = seal.next_period
    stored = anchor.require(seal.estate_id)
    head = stored.head
    # ss11's never-opened row: the head already says this root opened this
    # period, and only the SEGMENT is missing. The claim was consumed when
    # the head moved, so re-taking it would refuse -- what is left to do is
    # write the segment the crash cost us, which is byte-identical (PR-07).
    reopening = (
        isinstance(head, OpenHead)
        and head.period_id == opening.period_id
        and normalized_root(head.root) == normalized_root(run_root)
    )
    claim = (
        None
        if reopening
        else anchor.claim_successor(
            estate_id=seal.estate_id,
            seal_digest=seal.digest,
            next_period=opening.period_id,
            target_root=run_root,
        )
    )
    crash_point("after_claim")
    if callable(catalog):
        catalog = catalog()
    link = {"period_id": seal.period_id, "digest": seal.digest}
    # ss1.3: a break-glass override is recorded in the anchor AND in the
    # next `segment`. Copied here rather than consumed, so the fork stays
    # visible in both places for the life of both artifacts
    forced = pending_reclaim(stored, opening.period_id)
    path = open_wal(run_root, opening.segment_no)
    if path.exists():
        _check_existing_segment(path, opening, link, forced)
        # a previous opener may have died between its write and its fsync:
        # readable is not durable, and the CAS below relies on this file
        fsync_file(path)
        journal = Journal(
            path,
            fsync_each=opening.clock_domain == "real",
            baseline_id=opening.baseline_id,
            lock=lock,
        )
    else:
        journal = Journal.create(
            path,
            catalog=catalog,
            clock_domain=opening.clock_domain,
            started_at=seal.closed_at,
            lock=lock,
            manifest=committed.manifest,
            estate_id=seal.estate_id,
            opens_from_seal=link,
            reclaimed=None if forced is None else forced.model_dump(mode="json"),
        )
        fsync_dir(path.parent)
    crash_point("after_opening_segment")
    if claim is not None:
        anchor.open_claimed(claim_id=claim.claim_id, period_id=opening.period_id, root=run_root)
    crash_point("after_open_cas")
    return OpenedPeriod(
        opened=open_from_seal(seal, expected_digest=seal.digest, manifest=committed.manifest),
        journal=journal,
        claim=claim,
    )


def pending_reclaim(anchor: Anchor, next_period: int) -> Reclaimed | None:
    """The break-glass override that cleared this opening's way, or None.

    The newest entry for this period, because a lineage can be reclaimed
    more than once and the opener that gets through is the one the LAST
    reclaim let through."""
    for entry in reversed(anchor.reclaimed):
        if entry.next_period == next_period:
            return entry
    return None


def _check_existing_segment(
    path: Path,
    opening: CommittedNextPeriod,
    link: Mapping[str, Any],
    forced: Reclaimed | None = None,
) -> None:
    """A segment already written by an interrupted opener is VERIFIED, not
    appended to: a second `segment` record for one period is exactly the
    two-candidate state I1 exists to make impossible."""
    records = read_journal(path)
    segment = records[0]
    # the DERIVED field set (DL-137): the old hand-written five omitted
    # the three content hashes, catalog_hash_version and
    # state_machine_version -- strictly weaker than
    # `check_manifest_against_segment` for the same question, by accident
    # rather than argument
    shared = sorted(set(type(opening).model_fields) & SEGMENT_FIELDS)
    disagree = [
        f"{field}: segment {found!r} vs the boundary's {ours!r}"
        for field, found, ours in disagreements(segment, opening, shared)
    ]
    if segment.get("opens_from_seal") != dict(link):
        disagree.append(
            f"opens_from_seal: segment {segment.get('opens_from_seal')!r} vs {dict(link)!r}"
        )
    stamped = None if forced is None else forced.model_dump(mode="json")
    if segment.get("reclaimed") != stamped:
        disagree.append(f"reclaimed: segment {segment.get('reclaimed')!r} vs {stamped!r}")
    if disagree:
        raise EngineError(
            f"{path} already holds a segment this boundary did not open"
            f" ({'; '.join(disagree)}): a segment whose pins disagree with the"
            " preceding seal's next_period is refused (period-model ss3.4)"
        )


# ------------------------------------------------------------- resume


def read_seal(run_root: Path, period_id: int) -> Seal:
    """The sidecar for `period_id`, parsed through the versioned schema.

    A committed seal whose sidecar is MISSING refuses: the boundary is
    unrecoverable, and there is nothing honest to degrade to (ss11)."""
    path = seal_path(run_root, period_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise EngineError(
            f"{path}: a committed seal names a sidecar that is not there --"
            " this boundary is unrecoverable (period-model ss11)"
        ) from exc
    except OSError as exc:
        raise EngineError(f"{path}: unreadable: {exc}") from exc
    return Seal.from_bytes(raw)


@dataclass(frozen=True)
class Lineage:
    """ss11 step 3's answer: which seal this root's replay starts from, and
    whether that seal still has to be OPENED."""

    #: None only in period 1 before any seal exists -- draft 3 said "never
    #: from genesis" and left a new-format estate that crashes before its
    #: first seal with no path back
    seal: Seal | None
    #: True when the seal is committed and no successor segment exists: the
    #: boundary is done and the opening is not
    opens_next: bool

    def __post_init__(self) -> None:
        if self.opens_next and self.seal is None:
            # select_seal only sets opens_next True on the committed-seal
            # branch (below), which always reads a seal first -- opens_next
            # without a seal to open is a Lineage no caller of select_seal
            # can construct (DL-171)
            raise AssertionError("opens_next requires seal (Lineage invariant, DL-171)")

    def target_period(self, fallback: int) -> int:
        """period-model ss11 step 5: the period a resume of this root opens
        into -- the newest segment's period (`fallback`, the caller's to
        compute), unless this root holds a COMMITTED boundary no engine has
        opened yet, in which case it is the seal's next period."""
        if self.opens_next:
            assert self.seal is not None  # structural: __post_init__ above
            return self.seal.next_period.period_id
        return fallback


def select_seal(run_root: Path, records: Sequence[Mapping[str, Any]]) -> Lineage:
    """ss11 step 3: select the seal BY LINEAGE, from what this root holds.

    A rolled root never holds the predecessor's WAL or `seal` record, so a
    rule that walked to "the newest committed `seal` record" would select
    evidence it does not have; the local `segment` is the proof. In every
    case the sidecar's recomputed digest is verified against the digest the
    NAMING RECORD carries, and every duplicated field with it -- a matching
    digest proves integrity, never derivation."""
    committed = [record for record in records if record.get("rec") == "seal"]
    if committed:
        record = committed[-1]
        check_seal_record(record)
        seal = read_seal(run_root, int(record["period_id"]))
        check_record_names_sidecar(seal, record, run_root)
        return Lineage(seal=seal, opens_next=True)
    link = records[0].get("opens_from_seal") if records else None
    if isinstance(link, Mapping):
        seal = read_seal(run_root, int(link["period_id"]))
        if seal.digest != link.get("digest"):
            raise EngineError(
                f"{seal_path(run_root, int(link['period_id']))}: digest {seal.digest} but"
                f" the segment that opened from it says {link.get('digest')!r} -- an"
                " orphan or a stranger's sidecar (period-model ss11)"
            )
        return Lineage(seal=seal, opens_next=False)
    return Lineage(seal=None, opens_next=False)


def check_record_names_sidecar(seal: Seal, record: Mapping[str, Any], run_root: Path) -> None:
    """Every duplicated field of the `seal` RECORD equals the sidecar's
    (ss2.2/ss11): recovery selects the sidecar by these fields, audit
    refuses a rewritten record before re-derivation attests the stored
    one. One name (DL-137): this was a three-symbol forwarding chain."""
    disagree = _record_disagreements(seal, record)
    if disagree:
        raise EngineError(
            f"{seal_path(run_root, seal.period_id)} disagrees with the `seal` record that"
            f" names it ({'; '.join(disagree)}): a self-consistent sidecar that is"
            " not this boundary's is refused (period-model ss11)"
        )


def act_on_head(
    anchor: EstateAnchor,
    *,
    run_root: Path,
    estate_id: str,
    lineage: Lineage,
) -> Anchor:
    """ss11 step 4: act on the head before anything is replayed.

    Four rows, each repairing exactly one crash window:
    `open(N, this root)` with N's `seal` record present performs the CAS
    the crashed sealer did not; `closed` with no following segment leaves
    the claim to the opener; `claimed` with our claim and a durable segment
    that AGREES with the seal it opened from moves the head to `open`;
    `claimed` with another refuses, naming the holder; and `open(1, this
    root)` with a provisional row and a durable segment finalizes.

    Agreement, not presence, and the check runs BEFORE the CAS -- the same
    standard `open_next_period` holds an already-written segment to
    (DL-145)."""
    current = anchor.require(estate_id)
    head = current.head
    if (
        lineage.seal is not None
        and lineage.opens_next
        and isinstance(head, OpenHead)
        and head.period_id == lineage.seal.period_id
    ):
        # the seal record landed and the head did not move (PR-02b). The
        # period check is what keeps this off the neighbouring row: a head
        # already `open` at the SUCCESSOR has moved past this seal, and its
        # missing segment is the opener's business, not this CAS's.
        #
        # Fsync the CLOSING WAL first: the crashed sealer may have written
        # the line and died before its fsync completed, so "recovery read
        # it" proves readable, not durable -- and a CAS over a line a power
        # cut then removes leaves a successor whose naming seal is gone.
        # Any failure here stays fail-stopped: recovery mutates nothing it
        # has not first made durable.
        fsync_file(wal_path(run_root, lineage.seal.period_id))
        return anchor.close_period(
            estate_id=estate_id,
            period_id=lineage.seal.period_id,
            root=run_root,
            seal_digest=lineage.seal.digest,
        )
    if isinstance(head, ClaimedHead):
        if normalized_root(head.target_root) != normalized_root(run_root):
            raise EngineError(
                f"{anchor.path}: claim {head.claim_id} is held by {head.target_root}, not"
                " by this root: a second opener forks the lineage -- `dsl41 estate"
                " reclaim --force` is the break-glass (period-model ss1.3)"
            )
        claim = anchor.read_claim(head.claim_id)
        if claim is None:
            raise EngineError(
                f"{anchor.path}: the head names claim {head.claim_id} and"
                f" {anchor.claim_path(head.claim_id)} is not there -- the claim file is"
                " written before the head moves, so this state is unreachable without"
                " something deleting it (period-model ss1.3)"
            )
        segment = wal_path(run_root, claim.next_period)
        if segment.exists():
            # ONE evidence standard for the head move (DL-145). The CAS
            # used to run on the segment's PRESENCE while `open_next_period`
            # demands AGREEMENT -- `_check_existing_segment` first, then the
            # CAS -- and the OPERATOR's route reaches this one: a crash
            # between `after_opening_segment` and `after_open_cas` is
            # repaired here, at resume, so a segment whose pins disagree
            # with the seal moved the head and was caught afterwards.
            # DL-142's pinned check-before-CAS order now holds on both
            # routes.
            seal = lineage.seal
            if seal is None or seal.next_period.period_id != claim.next_period:
                raise EngineError(
                    f"{anchor.path}: the head claims period {claim.next_period} for"
                    f" {run_root}, and the seal this root's records name opens"
                    f" {'nothing' if seal is None else f'period {seal.next_period.period_id}'}"
                    " -- the head move is licensed by the seal the claimed segment"
                    " opened from, and this root holds no seal that opens the"
                    " claimed period (period-model ss11)"
                )
            _check_existing_segment(
                segment,
                seal.next_period,
                {"period_id": seal.period_id, "digest": seal.digest},
                pending_reclaim(current, claim.next_period),
            )
            # same readable-vs-durable rule as the close CAS above: the
            # segment's existence justifies the head move, so the segment
            # is made durable before the head names it
            fsync_file(segment)
            return anchor.open_claimed(
                claim_id=claim.claim_id, period_id=claim.next_period, root=run_root
            )
        return current
    if isinstance(head, OpenHead) and wal_path(run_root, head.period_id).exists():
        fsync_file(wal_path(run_root, head.period_id))  # same rule at genesis finalize
        return anchor.finalize(head.period_id)
    return current
