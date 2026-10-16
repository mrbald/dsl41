"""The boundary: the lineage anchor, the genesis transaction, and the seal operation.

Normative spec: `docs/period-model.md` ss1.1 (sentinel, genesis), ss1.2/ss1.3 (estate identity,
successor fence), ss6 (cutoff barrier), ss7 (seal operation, three pure phases), ss8
(preconditions), ss9 (retries across a boundary), ss11 steps 1-4 (resume). DL-133 (U6b). Obligations
PR-01a/b/c, PR-02, PR-03, PR-04, PR-25 through PR-33 in ss13.

`seal.py` holds the artifact and two pure functions over it. This module is the operation: what
must hold before a period closes, the order bytes hit disk, which writer performs the cutoff,
and who may open next. It calls `close_runtime` and `open_from_seal` unchanged.

The anchor is the fence. A run root is a path and identifies nothing (ss1); two roots holding a
committed seal with no following segment would both open the next period and run the same
`(job, run_number)` twice, violating `concurrency-model.md` ss0. Exactly one root may succeed a
seal. The anchor decides it: a four-state head under a lock, moved by compare-and-swap, with a
durable claim file so a crashed claimant's replacement proves it is the same claim.
`LeaderLock`'s pattern is used, not a bare `O_EXCL`, because it solves replacement and lifetime.

The head moves after the fact it records. Genesis writes its registry row before any segment
exists, marked provisional; a successor's row is written in the same anchor write as
`claimed -> open`, after its first segment is durable (PR-02c).

A seal is three writes in order (ss3): the sidecar, the `seal` record, the anchor CAS. A crash
between the first two leaves an orphan sidecar that recovery ignores; between the last two,
resume performs the CAS. The `seal` append is the point of no return: once any of its bytes may
have been written the engine fail-stops rather than append records after a seal line.

`abort_boundary` runs on every non-commit exit before that append (PR-28b). A fence loss inside
the interval fail-stops instead, on DL-101's rule.

A pre-PONR refusal carries the control code `seal_refused`, or `seal_retry_horizon` for the ss9
gate (control-protocol ss2). Some of these checks also serve audit and retention, where the
code is unread.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid

from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal, TypeVar

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
    not_ours,
    own_or_refuse,
    period_dir,
    quarantine_dir,
    read_or_none,
    read_sentinel,
    require_manifest_fields,
    runtime_hash,
    seal_dir,
    seal_path,
    sentinel_path,
    stage_manifest,
    staging_dir,
    switches_of,
    tz_aliases_of,
    is_opening,
    wal_path,
    wal_segments,
    write_bundle,
    write_period_manifest,
    write_sentinel,
    SEGMENT_FIELDS,
)
from dsl41.oracle_state import LIVE, JobRuntime
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
from dsl41.state_machine import Event as ChartEvent
from dsl41.state_machine import Executor, Region, Row, State, Statechart, StatechartError

#: ss1.1's anchor directory, outside any archivable root.
ANCHOR_NAME: Final[str] = "anchor.json"
ANCHOR_LOCK_NAME: Final[str] = "anchor.lock"
CLAIMS_DIR: Final[str] = "claims"

#: ss1.3: local filesystem only; a network-filesystem anchor is refused at startup (PR-04).
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

#: Seconds the sealer waits out an unbound SPAWN and an unresolved KILL ladder instead of
#: snapshotting one (ss3.5, ss8, PR-27, PR-33a); a wedged tier refuses after this.
QUIESCE_WAIT_S: Final[float] = 30.0


class PeriodSealed(Exception):
    """ss7: the boundary committed; period N+1 is ready to open.

    Not an `EngineError`: it is a successful terminal outcome and every `except EngineError`
    is a failure path. `run` exits 3 (PR-30b) so an init system does not restart-loop a sealed
    engine; step 9 is `dsl41 run --resume` on the same root."""

    def __init__(self, boundary: CommittedBoundary) -> None:
        super().__init__(
            f"sealed period {boundary.seal.period_id} at {boundary.seal.digest};"
            f" period {boundary.seal.next_period.period_id} is ready to open"
        )
        self.boundary = boundary


class BoundaryFailStop(EngineError):
    """The `seal` append did not complete cleanly (ss7).

    An `fsync` error does not prove the line absent, and a partial append may leave a torn final
    line. Reopening C1 would append records after a seal line that survives a crash, which
    recovery refuses. Once any seal bytes may have been written the engine fail-stops and
    reports the outcome unknown; recovery decides (PR-28d)."""


class BoundaryRefusal(EngineError):
    """A pre-PONR refusal of the boundary, with control code `seal_refused`
    (control-protocol ss2). Some checks that raise it also serve audit and retention, where
    the code is unread."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="seal_refused")


class SealRequest(BaseModel):
    """ss2.2's v3 `seal` request, as the engine receives it.

    It has no `expect`, because a boundary is not a row mutation, and it carries `request_id`
    like every command, so a lost response is retryable. `source` has one value (a live and an
    offline seal are one kind of boundary); audit derives and compares it (DL-138).

    `strict=True` (DL-170) stops `epoch=True` coercing to `1` (DL-151). `cli_estate.py`'s
    offline retry route builds this model past `runner_control._seal_wire_error`, so the flag
    is defence in depth there. `StagedNextPeriod` carries its own `strict=True` (DL-168)."""

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
    """The lineage a live engine leads: which estate this process is period N of. An engine
    without one (the bisimulation harness, a rehearsal) has nothing a seal request can close."""

    run_root: Path
    anchor: EstateAnchor
    estate_id: str
    #: the closing period's committed manifest; ss9's gate reads `retry_horizon_us` from it
    manifest: Manifest
    #: the seal this period opened from, `prev_seal_digest` on the next
    prev_seal_digest: str | None = None
    #: ss2.2's retry route: the `seal` record this period opened from, so an exact retry of the
    #: boundary that closed C1 is answered before the current-baseline check; the generic v3
    #: parser would reject its foreign `baseline_id` (B1 against C2's B2) first
    prior_seal_record: dict[str, Any] | None = None


def no_crash(_stage: str) -> None:
    """The crash matrix's seam: a no-op in production.

    Every durable step calls it with a stage name, so ss11a's crash matrix stops the operation
    exactly between two writes."""


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

    `closing_root` lets a physical roll's opener find the sidecar, the C2 bundle and the
    period manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["closed"] = "closed"
    period_id: int = Field(ge=1)
    seal_digest: str
    closing_root: str


class ClaimedHead(BaseModel):
    """One root is opening the next.

    Keyed on the claim, never the claimant: a crashed claimant's replacement has a different
    pid, and recovery must not need `--force`, which can fork a lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["claimed"] = "claimed"
    claim_id: str
    target_root: str


#: Retired lineage head states: state -> the entry that retired it. Append-only
#: (docs/protocol-evolution.md ss6). An anchor carrying one is refused before parse, because the
#: discriminated union's validator error names none of this.
RETIRED_HEAD_STATES: Final[dict[str, str]] = {"adopting": "DL-138"}

Head = Annotated[OpenHead | ClosedHead | ClaimedHead, Field(discriminator="state")]


class PeriodRow(BaseModel):
    """ss1.3's archive registry entry: the root that holds this period and what is proved.

    A row is provisional until `segment_durable`; genesis inserts it before any segment
    exists and cross-period readers ignore it until then."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: str
    segment_durable: bool = False
    seal_digest: str | None = None
    attested: bool = False


class Reclaimed(BaseModel):
    """One break-glass override, as the anchor records it (ss1.3): the moved claim, its root,
    the actor and the time. It is reported in the anchor and in the opening `segment` of the
    period let through; it decides nothing."""

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
    #: keyed by the period number as a string (JSON has no integer keys)
    periods: dict[str, PeriodRow] = {}
    #: ss1.3's break-glass ledger of claims `estate reclaim --force` moved aside, in order.
    #: Append-only; the next opening `segment` copies the entry into its own `reclaimed` field
    reclaimed: list[Reclaimed] = []

    def row(self, period_id: int) -> PeriodRow | None:
        return self.periods.get(str(period_id))

    def with_row(self, period_id: int, row: PeriodRow) -> dict[str, PeriodRow]:
        return {**self.periods, str(period_id): row}


class Claim(BaseModel):
    """ss1.3's `claims/<claim_id>.json`: the durable successor claim.

    Identity is the claim, not the process; `diag` only tells an operator who was here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_format_version: int = ARTIFACT_FORMAT_VERSION
    claim_id: str
    estate_id: str
    prev_seal_digest: str
    next_period: int = Field(ge=2)
    target_root: str
    #: ss3.2's spelling: naive UTC with exactly six fractional digits
    claimed_at: str
    diag: dict[str, Any] = {}


def normalized_root(root: Path | str) -> str:
    """ss1.3: `target_root` is the absolute normalized path, so a claimant restarted under
    another spelling computes the same `claim_id`."""
    return os.path.realpath(str(root))


def claim_id_for(*, prev_seal_digest: str, next_period: int, target_root: Path | str) -> str:
    """ss1.3: `sha256(canonical{prev_seal_digest, next_period, target_root})`.

    `next_period` is the number only; the seal digest already binds the opening."""
    return hash_over(
        {
            "next_period": next_period,
            "prev_seal_digest": prev_seal_digest,
            "target_root": normalized_root(target_root),
        }
    )


def filesystem_type(path: Path) -> str | None:
    """The filesystem `path` sits on, or None when this platform will not say.

    Read from `/proc/mounts`, else `mount(8)`; the longest mount point prefixing the real path
    wins. None is an unknown, not a refusal."""
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
    """PR-04: an NFS anchor is refused at startup, because `flock` there is not a fence."""
    kind = filesystem_type(anchor_dir)
    if kind is not None and kind.lower() in NETWORK_FILESYSTEMS:
        raise EngineError(
            f"{anchor_dir} is on {kind}: the anchor's flock is not a fence on a network"
            " filesystem, so two roots could both claim one seal -- put the anchor on"
            " local storage (period-model ss1.3, PR-04)"
        )


# ------------------------------------------------- the anchor's machines
#
# `ANCHOR_HEAD` and `PERIOD_ROW` are declarations (docs/statechart-engine.md). Their state lives
# in `anchor.json`: the head's `state` is the head instance's configuration, a registry row's
# flags (`row_tag`) its row instance's. Every move is a read-compare-write under the anchor
# lock; its executor lives for that one move (`_Move`), built from the file read under the lock
# and posted the method's event, head first and row second, then settled before the method's
# one anchor write (a claim writes its claim file first). No file or row means no instance.
#
# A refusal row is a cell with no move; the method raises its `EngineError` for the row's code.
# The method runs the checks that read its own facts before the post, and an idempotent repeat
# posts nothing. A gap in a chart is a `StatechartError`, and so is a write whose tag is not
# where the chart rests (DL-336).

#: A registry row's states. `provisional` is a row whose segment is not durable
#: yet, `durable` one whose segment is, `attested` one `audit` has certified;
#: `absent` is no row, so no instance.
type RowTag = Literal["absent", "provisional", "durable", "attested"]

#: The cite of most of the anchor's rows.
_SS13: Final = "period-model ss1.3"

#: The lineage head, one instance per anchor directory. Its leaves are the
#: tags of `Head`; `absent` is no file, so no instance.
ANCHOR_HEAD: Final = Statechart(
    name="anchor_head",
    root=State(
        "anchor_head",
        regions=(Region("head", "open", (State("open"), State("closed"), State("claimed"))),),
    ),
    rows=(
        Row(
            "anchor_head.02",
            "open",
            "close_period",
            "closed",
            cite=f"{_SS13}, ss3",
            note=(
                "The head is open at this period (the method's check). Write it closed at"
                " the seal digest, with the period's row in the same write."
            ),
        ),
        Row(
            "anchor_head.03",
            "closed",
            "claim_successor",
            "claimed",
            cite=_SS13,
            note=(
                "The head is closed at this seal digest (the method's check). Write the"
                " claim file first, then the head."
            ),
        ),
        Row(
            "anchor_head.04",
            "claimed",
            "open_claimed",
            "open",
            cite=f"{_SS13}, PR-02c",
            note=(
                "The head names this claim (the method's check). Write the successor's"
                " durable registry row in the same write."
            ),
        ),
        Row(
            "anchor_head.05",
            "claimed",
            "reclaim",
            "closed",
            cite=_SS13,
            note=(
                "The claim file binds its body, the estate and the head, and the registry"
                " holds the previous seal as committed (the method's checks). Append a"
                " Reclaimed entry and return the head to the previous seal."
            ),
        ),
        Row(
            "anchor_head.06",
            "claimed",
            "claim_successor",
            kind="internal",
            cite=f"{_SS13}, DL-290",
            note=(
                "The head names this claim and the claim file is gone (the method's"
                " checks). Write the claim file again; the head is written unchanged."
                " DL-290 leaves open whether this is reachable: `reclaim` and the resume"
                " step refuse the same state as unreachable."
            ),
        ),
        Row(
            "anchor_head.07",
            "anchor_head",
            "create_open",
            kind="refusal",
            code="anchor_exists",
            cite="period-model ss1.1, PR-01b",
            note="The method answers its own interrupted genesis before any post.",
        ),
        Row(
            "anchor_head.08",
            "anchor_head",
            "close_period",
            kind="refusal",
            code="head_not_open",
            cite=_SS13,
            note="The method answers a repeat at this period's closed head before any post.",
        ),
        Row(
            "anchor_head.09",
            "anchor_head",
            "claim_successor",
            kind="refusal",
            code="head_not_closed",
            cite=_SS13,
        ),
        Row(
            "anchor_head.10",
            "anchor_head",
            "open_claimed",
            kind="refusal",
            code="head_not_claimed",
            cite=_SS13,
            note="The method answers a repeat at this period's open head before any post.",
        ),
        Row(
            "anchor_head.11",
            "anchor_head",
            "reclaim",
            kind="refusal",
            code="head_not_claimed",
            cite=_SS13,
        ),
    ),
    clock="logical",
)

#: A row reached only from `attested`: `unattested` takes the event first.
_FROM_ATTESTED: Final = "Only `attested` reaches this row."
#: What a refusal row of `attested` meets when the method posts to it.
_FORGED: Final = "It meets a forged row only: attested without its durable flag."

#: One period's registry row, one instance per anchor directory and period id. A row is born
#: provisional at genesis, or durable when a successor opens.
PERIOD_ROW: Final = Statechart(
    name="period_row",
    root=State(
        "period_row",
        regions=(
            Region(
                "row",
                "unattested",
                (
                    State(
                        "unattested",
                        regions=(
                            Region(
                                "segment", "provisional", (State("provisional"), State("durable"))
                            ),
                        ),
                    ),
                    State("attested", final=True),
                ),
            ),
        ),
    ),
    rows=(
        Row(
            "period_row.02",
            "unattested",
            "close_period",
            "durable",
            cite=f"{_SS13}, ss3",
            note=(
                "The head closes this period. Record the seal digest and mark the segment"
                " durable. `provisional` is a tolerated source, not reachable after a crash:"
                " the row is present and finalized before any run. With no row the close"
                " writes one, durable, and no instance existed to move."
            ),
        ),
        Row(
            "period_row.04",
            "provisional",
            "finalize",
            "durable",
            cite=_SS13,
            note="The segment has landed. Mark it durable.",
        ),
        Row(
            "period_row.05",
            "durable",
            "attest",
            "attested",
            cite=f"{_SS13}, ss11",
            note=(
                "The row's seal digest and root match the audit (the method's checks). Mark"
                " the row attested."
            ),
        ),
        Row(
            "period_row.06",
            "period_row",
            "create_open",
            kind="refusal",
            code="row_exists",
            cite=f"{_SS13}, PR-02c",
            note="The head refuses first: genesis meets an existing anchor.",
        ),
        Row(
            "period_row.07",
            "period_row",
            "open_claimed",
            kind="refusal",
            code="row_exists",
            cite=f"{_SS13}, PR-02c",
            note="A successor's row is written only in the write that opens it.",
        ),
        Row(
            "period_row.08",
            "period_row",
            "close_period",
            kind="refusal",
            code="row_attested",
            cite=_SS13,
            note=_FROM_ATTESTED,
        ),
        Row(
            "period_row.09",
            "durable",
            "finalize",
            kind="refusal",
            code="row_durable",
            cite=_SS13,
            note="The method answers this repeat before any post.",
        ),
        Row(
            "period_row.10",
            "period_row",
            "finalize",
            kind="refusal",
            code="row_attested",
            cite=_SS13,
            note=_FROM_ATTESTED + " The method answers a durable row before any post. " + _FORGED,
        ),
        Row(
            "period_row.11",
            "provisional",
            "attest",
            kind="refusal",
            code="row_not_durable",
            cite=_SS13,
        ),
        Row(
            "period_row.12",
            "period_row",
            "attest",
            kind="refusal",
            code="row_attested",
            cite=_SS13,
            note=_FROM_ATTESTED + " The method answers a repeat before any post. " + _FORGED,
        ),
    ),
    clock="logical",
)


def head_configuration(tag: str) -> frozenset[str]:
    """The configuration a head tag projects from: what `_Move` restores."""
    return frozenset({"anchor_head", tag})


def row_configuration(tag: str) -> frozenset[str]:
    """The configuration a registry row's tag projects from: what `_Move` restores."""
    if tag == "attested":
        return frozenset({"period_row", "attested"})
    return frozenset({"period_row", "unattested", tag})


def row_tag(row: PeriodRow | None) -> RowTag:
    """The registry row's state: `absent` for no row."""
    if row is None:
        return "absent"
    if row.attested:
        return "attested"
    return "durable" if row.segment_durable else "provisional"


class _Move:
    """One move's executor (statechart-engine ss11), built from the anchor read under the lock.

    It holds the head instance (keyed by the anchor directory) and the moved period's row
    instance (keyed by directory and period id). Every post of one move carries its one
    event."""

    def __init__(
        self, anchor_dir: Path, event: str, anchor: Anchor | None, period_id: int | None = None
    ) -> None:
        self.machines = Executor()
        self.event = ChartEvent(event)
        self.head: Path = anchor_dir
        self.row: tuple[Path, int | None] = (anchor_dir, period_id)
        self.period_id = period_id
        self.held: dict[Hashable, Statechart] = {}
        if anchor is not None:
            self._restore(self.head, ANCHOR_HEAD, head_configuration(anchor.head.state))
            row = None if period_id is None else anchor.row(period_id)
            if row is not None:
                self._restore(self.row, PERIOD_ROW, row_configuration(row_tag(row)))

    def _restore(self, key: Hashable, chart: Statechart, active: frozenset[str]) -> None:
        self.machines.restore(key, chart, active)
        self.held[key] = chart

    def post(self, key: Hashable, refusals: Mapping[str, str]) -> None:
        """Post the move's event to one instance, when it exists. A refusal raises the method's
        `EngineError` for its code; a code without text is a `StatechartError`."""
        if key not in self.held:
            return
        (outcome,) = self.machines.dispatch(key, self.event)
        if not outcome.refusals:
            return
        (code,) = outcome.refusals
        if code not in refusals:
            error = StatechartError(
                f"{self.held[key].name} {key!r}: the refusal {code} of {self.event.name}"
                " has no text in the anchor's method",
                candidates=outcome.rows,
            )
            raise self.machines.explain(error, key, self.event)
        raise EngineError(refusals[code])

    def settle(self, updated: Anchor) -> None:
        """Before the write: every instance rests in the tag the write records, and a created
        instance is restored in it. Any other tag is a `StatechartError` and nothing is
        written."""
        tags: list[tuple[Hashable, Statechart, frozenset[str]]] = [
            (self.head, ANCHOR_HEAD, head_configuration(updated.head.state))
        ]
        if self.period_id is not None:
            row = updated.row(self.period_id)
            tags.append((self.row, PERIOD_ROW, row_configuration(row_tag(row))))
        for key, chart, written in tags:
            if key not in self.held:
                self._restore(key, chart, written)
                continue
            active = self.machines.active(key)
            if active != written:
                error = StatechartError(
                    f"{chart.name} {key!r}: the move writes {sorted(written)} and the chart"
                    f" rests in {sorted(active)}"
                )
                raise self.machines.explain(error, key, self.event)


class EstateAnchor:
    """ss1.3's lineage authority: a directory holding `anchor.json`, `anchor.lock` and
    `claims/`.

    Whoever leads the lineage (the engine live, the sealer offline) holds the lock for its
    process lifetime, and every transition is a read-compare-write under it. The lock excludes
    another process; the compare excludes this one acting on a head it did not read."""

    def __init__(self, anchor_dir: Path) -> None:
        self.dir = Path(anchor_dir)
        self.path = self.dir / ANCHOR_NAME
        self.lock = LeaderLock(self.dir, ANCHOR_LOCK_NAME, of="estate lineage", held_by="process")

    def acquire(self) -> None:
        # as for the run root: every entry down to the anchor directory is made durable,
        # unconditionally, so a retry after a failed fsync repairs it
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

        A present but unreadable anchor refuses, because genesis may create over "no anchor"."""
        raw = read_or_none(self.path)
        if raw is None:
            return None
        try:
            payload = decode(raw)
            if not isinstance(payload, dict):
                raise EngineError(f"{self.path}: not a JSON object")
            require_artifact_version(payload)  # DL-157
            _check_head_state(self.path, payload)
            # DL-263: strict JSON from `raw`, as `_read_artifact` does (DL-168); `decode` runs
            # first so the gates inspect the payload pydantic validates. A laundered
            # `head.period_id` (`true`) refuses.
            return Anchor.model_validate_json(raw, strict=True)
        except (CanonError, ValidationError) as exc:
            raise EngineError(f"{self.path}: not an anchor this binary can read ({exc})") from exc

    def write(self, anchor: Anchor) -> None:
        """One head transition, one liturgy write (ss1.3).

        `durable_write` fsyncs the file and the directory entry; a rename alone could revert
        the head after B's first segment survived, and a second target would claim the same
        seal (PR-02c)."""
        self.lock.check()
        durable_write(str(self.path), canonical_bytes(anchor.model_dump(mode="json")) + b"\n")

    def claim_path(self, claim_id: str) -> Path:
        return self.dir / CLAIMS_DIR / f"{claim_id.split(':')[-1]}.json"

    def read_claim(self, claim_id: str) -> Claim | None:
        path = self.claim_path(claim_id)
        raw = read_or_none(path)
        if raw is None:
            return None
        try:
            payload = decode(raw)
            if not isinstance(payload, dict):
                raise EngineError(f"{path}: not a JSON object")
            require_artifact_version(payload)  # DL-157
            # DL-263: strict JSON from `raw`, as in `EstateAnchor.read`; a laundered
            # `next_period` (a numeric string) refuses.
            return Claim.model_validate_json(raw, strict=True)
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

        Create-only under ss1.1's ownership rule, with one resume exception: our own
        `open(1, this root, this estate_id)` with no committed segment, which is genesis
        interrupted. Once a segment exists, `--resume` owns recovery and this refuses. Every
        existing head refuses `create_open` (anchor_head.07, PR-01b). The write creates the
        head `open` and the row `provisional`."""
        existing = self.read()
        ours = (
            existing is not None
            and existing.estate_id == estate_id
            and isinstance(existing.head, OpenHead)
            and existing.head.period_id == period_id
            and normalized_root(existing.head.root) == normalized_root(root)
            and not (existing.row(period_id) or PeriodRow(root="")).segment_durable
        )
        if ours:
            assert existing is not None
            return existing
        move = _Move(self.dir, "create_open", existing, period_id)
        move.post(
            move.head,
            {"anchor_exists": not_ours(what=str(self.path), holder=_holder_of(existing))},
        )
        anchor = Anchor(
            estate_id=estate_id,
            head=OpenHead(period_id=period_id, root=normalized_root(root)),
            # provisional until the segment lands; readers ignore it until `segment_durable`
            # (PR-02c)
            periods={str(period_id): PeriodRow(root=normalized_root(root))},
        )
        move.settle(anchor)
        self.write(anchor)
        return anchor

    def attest(
        self, period_id: int, *, estate_id: str, root: Path | str, seal_digest: str
    ) -> Anchor:
        """The `attested` transition, owned by `audit` (ss1.3): one write under the lock, after
        `audit.json` is durable.

        Idempotent, so a re-run of `audit` does not refuse the flag it set. Bound to one
        estate's period in one root, so a stranger's anchor is never marked attested."""
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
        # exact (null included) and durable: a `seal` record can land before the close CAS, and
        # attesting that provisional row would certify an uncommitted boundary (period-model
        # ss1.3). The digest is this method's check; durability is the chart's (period_row.11)
        uncommitted = (
            f"{self.path}: period {period_id} has seal {row.seal_digest!r}"
            f" (segment_durable={row.segment_durable}) and the attestation names"
            f" {seal_digest} -- only a committed, durable row is attested"
            " (period-model ss1.3)"
        )
        if row.seal_digest != seal_digest:
            raise EngineError(uncommitted)
        if row.attested and row.segment_durable:
            return anchor
        # a row attested without its durable flag is forged and refuses as uncommitted
        # (period_row.12)
        move = _Move(self.dir, "attest", anchor, period_id)
        move.post(move.row, {"row_not_durable": uncommitted, "row_attested": uncommitted})
        updated = anchor.model_copy(
            update={
                "periods": anchor.with_row(period_id, row.model_copy(update={"attested": True}))
            }
        )
        move.settle(updated)
        self.write(updated)
        return updated

    def reclaim(self, *, estate_id: str, claimed_actor: str) -> tuple[Anchor, Reclaimed]:
        """ss1.3's break-glass: move a `claimed` head back to `closed` so another root may claim
        the seal.

        A stale claim is break-glass, not garbage: an unreachable target cannot be told from a
        paused one, so an operator decides, under `--force`. The moved claim and the claiming
        actor come back for the next opening `segment`'s `reclaimed`. It is the one path here
        that can fork a lineage.

        The seal the head returns to is the claim's `prev_seal_digest` and the closing root is
        the registry's row, both read and never supplied. The checks run only for a claimed
        head, before the post; every other head refuses (anchor_head.11)."""
        anchor = self.require(estate_id)
        head = anchor.head
        reclaimed = None
        if isinstance(head, ClaimedHead):
            reclaimed = self._reclaimed(anchor, head, estate_id, claimed_actor)
        move = _Move(self.dir, "reclaim", anchor)
        move.post(
            move.head,
            {
                "head_not_claimed": f"{self.path}: the head is {spell_head(head)}, not a claim --"
                " there is nothing to reclaim, and this verb never moves a head that is"
                " doing its job (period-model ss1.3)"
            },
        )
        if reclaimed is None:
            # anchor_head.11 refuses every other head; a chart that moves one disagrees with
            # this method
            error = StatechartError(
                f"anchor_head {move.head!r}: reclaim moved a head that is"
                f" {head.state}, and the method checks only a claimed one"
            )
            raise move.machines.explain(error, move.head, move.event)
        updated, moved = reclaimed
        move.settle(updated)
        self.write(updated)
        return updated, moved

    def _reclaimed(
        self, anchor: Anchor, head: ClaimedHead, estate_id: str, claimed_actor: str
    ) -> tuple[Anchor, Reclaimed]:
        """`reclaim`'s checks of the claim file and registry, and the anchor and ledger entry
        it writes."""
        claim = self.read_claim(head.claim_id)
        if claim is None:
            raise EngineError(
                f"{self.path}: the head names claim {head.claim_id} and"
                f" {self.claim_path(head.claim_id)} is not there -- the claim file is"
                " written before the head moves, so this state is unreachable without"
                " something deleting it (period-model ss1.3)"
            )
        # the claim's body must be what its name binds: the id derives from {prev_seal_digest,
        # next_period, target_root}, so a swapped body recomputes to another id (ss1.3)
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
        return updated, moved

    def finalize(self, period_id: int) -> Anchor:
        """Period 1's finalize CAS, performed right after its segment lands (ss1.3).

        Idempotent: an already-durable row writes nothing, so resume may call it."""
        anchor = self.require()
        row = anchor.row(period_id)
        if row is None:
            raise EngineError(
                f"{self.path}: period {period_id} has no registry row to finalize"
                " (period-model ss1.3)"
            )
        if row.segment_durable:
            return anchor
        move = _Move(self.dir, "finalize", anchor, period_id)
        move.post(
            move.row,
            {
                "row_attested": f"{self.path}: period {period_id}'s registry row is attested"
                " and its segment is not durable -- an attested row was durable first"
                " (period-model ss1.3)"
            },
        )
        updated = anchor.model_copy(
            update={
                "periods": anchor.with_row(
                    period_id, row.model_copy(update={"segment_durable": True})
                )
            }
        )
        move.settle(updated)
        self.write(updated)
        return updated

    def close_period(
        self, *, estate_id: str, period_id: int, root: Path, seal_digest: str
    ) -> Anchor:
        """`open -> closed`: the third write of the seal sequence (ss3).

        Idempotent on a head already closed at this digest, because recovery performs this CAS
        when the record landed and the head did not (PR-02b)."""
        anchor = self.require(estate_id)
        head = anchor.head
        if isinstance(head, ClosedHead) and head.period_id == period_id:
            if head.seal_digest != seal_digest:
                raise EngineError(
                    f"{self.path}: period {period_id} is already closed at"
                    f" {head.seal_digest} -- this seal says {seal_digest} (period-model ss1.3)"
                )
            return anchor
        cannot = (
            f"{self.path}: cannot close period {period_id}: the head is"
            f" {spell_head(head)} (period-model ss1.3)"
        )
        if isinstance(head, OpenHead) and head.period_id != period_id:
            raise EngineError(cannot)
        move = _Move(self.dir, "close_period", anchor, period_id)
        move.post(move.head, {"head_not_open": cannot})
        move.post(
            move.row,
            {
                "row_attested": f"{self.path}: cannot close period {period_id}: its registry"
                " row is already attested, and an attested row is closed (period-model ss1.3)"
            },
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
        move.settle(updated)
        self.write(updated)
        return updated

    def claim_successor(
        self, *, estate_id: str, seal_digest: str, next_period: int, target_root: Path
    ) -> Claim:
        """`closed -> claimed(claim_id, target_root)`, as one CAS (ss1.3).

        Idempotent on `claim_id`: the same `(seal, next_period, root)` resumes its own claim
        after a crash. A different `claim_id` against the same seal refuses, naming the holder;
        `estate reclaim --force` overrides it."""
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
        cannot = (
            f"{self.path}: cannot claim the successor of {seal_digest}: the head is"
            f" {spell_head(head)} (period-model ss1.3)"
        )
        if isinstance(head, ClosedHead) and head.seal_digest != seal_digest:
            raise EngineError(cannot)
        # checked before the claim file is written: anchor_head.03 from `closed`, .06 from
        # `claimed`
        move = _Move(self.dir, "claim_successor", anchor)
        move.post(move.head, {"head_not_closed": cannot})
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
        claimed = anchor.model_copy(
            update={"head": ClaimedHead(claim_id=claim_id, target_root=claim.target_root)}
        )
        move.settle(claimed)
        # the claim file first: the head may only name a claim that exists
        self.write_claim(claim)
        self.write(claimed)
        return claim

    def open_claimed(self, *, claim_id: str, period_id: int, root: Path) -> Anchor:
        """`claimed -> open`, after the new period's first `segment` record is durable, carrying
        the successor's registry row in the same write (ss1.3, PR-02c). It never runs earlier,
        or a crash would leave a row for a period with no segment."""
        anchor = self.require()
        head = anchor.head
        if isinstance(head, OpenHead) and head.period_id == period_id:
            return anchor  # the crash was after this very write
        cannot = (
            f"{self.path}: cannot open period {period_id} under claim {claim_id}:"
            f" the head is {spell_head(head)} (period-model ss1.3)"
        )
        if isinstance(head, ClaimedHead) and head.claim_id != claim_id:
            raise EngineError(cannot)
        move = _Move(self.dir, "open_claimed", anchor, period_id)
        move.post(move.head, {"head_not_claimed": cannot})
        move.post(
            move.row,
            {
                "row_exists": f"{self.path}: cannot open period {period_id} under claim"
                f" {claim_id}: the registry already holds a row for it -- a successor's row"
                " is written only in the write that opens it (period-model ss1.3, PR-02c)"
            },
        )
        updated = anchor.model_copy(
            update={
                "head": OpenHead(period_id=period_id, root=normalized_root(root)),
                "periods": anchor.with_row(
                    period_id, PeriodRow(root=normalized_root(root), segment_durable=True)
                ),
            }
        )
        move.settle(updated)
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


def spell_head(head: OpenHead | ClosedHead | ClaimedHead) -> str:
    """A head as a refusal names it: its state and what it carries (PR-01)."""
    if isinstance(head, OpenHead):
        return f"open(period {head.period_id}, {head.root})"
    if isinstance(head, ClosedHead):
        return f"closed(period {head.period_id}, {head.seal_digest}, {head.closing_root})"
    return f"claimed({head.claim_id}, {head.target_root})"


def _check_head_state(path: Path, payload: Mapping[str, Any]) -> None:
    """A retired head state refuses before the anchor is parsed (DL-138), because pydantic's
    unknown-tag error names neither what the operator holds nor why it is unreadable."""
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
    return f"estate {anchor.estate_id}, head {spell_head(anchor.head)}"


# --------------------------------------------------- the estate-wide walk


@dataclass(frozen=True)
class EstatePeriod:
    """One resolved registry row: the period, the root that holds it, and the row.

    `archived` is the period's archive receipt when it has one (ss12, DL-144). It is read
    whether or not the inputs are still on disk, because an archive is irreversible and every
    reader reports the tier the receipt names."""

    period_id: int
    root: Path
    row: PeriodRow
    archived: ArchiveReceipt | None = None


@dataclass(frozen=True)
class EstateWalk:
    """One lineage, root by root, in period order.

    `periods` holds an entry per registry row whose segment is durable. `provisional` holds
    the numbers of the others, which ss1.3 has readers ignore until the first segment lands."""

    anchor_dir: Path
    estate_id: str
    periods: tuple[EstatePeriod, ...]
    provisional: tuple[int, ...]

    def roots(self) -> tuple[Path, ...]:
        """Every root this lineage names, first appearance first, deduplicated."""
        seen: dict[Path, None] = {}
        for entry in self.periods:
            seen.setdefault(entry.root, None)
        return tuple(seen)


def is_anchor_dir(path: Path) -> bool:
    """Whether `path` addresses a lineage anchor rather than a run root.

    An anchor directory holds `anchor.json` and a run root `journal.jsonl`; ss1.1 puts the
    anchor outside every archivable root. `walk_estate` refuses a directory holding both."""
    return (Path(path) / ANCHOR_NAME).is_file()


def registry_rows(anchor: Anchor, *, anchor_dir: Path) -> list[tuple[int, PeriodRow]]:
    """ss1.3's archive registry as `(period, row)`, ascending: the one read of it (DL-145).

    Two rules, both refusing rather than dropping, because a dropped row makes a smaller estate
    look complete. Keys are canonical decimals only (`01` would alias `1`, as in
    `split_run_dir`). There are no holes: rows are inserted when a root first owns a period and
    never removed, so a gap is an edited anchor.

    Provisional rows are returned; the walk names them and the retention plan skips them.
    `anchor_dir` names the file in every refusal, because the model carries no path."""
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
    """ss1.3's archive registry as a lineage: every period this estate holds, in order, with
    its root (PR-02f). `audit`, `journal`, `runs` and `estate prune` take their roots from
    here, so none can silently report a smaller estate.

    A provisional row (genesis's, `segment_durable: false`) is ignored and reported in
    `provisional`, never refused. Every other row must resolve or the walk refuses by name,
    naming the period, the root and why: a missing root, no sentinel, an unreadable sentinel,
    another estate, or a missing segment.

    The anchor is read, never locked, for the reason `plan_retention` gives: a live engine
    holds the lineage lock for its lifetime. Each row is written before anything relies on it,
    so a row this read sees stays."""
    anchor_dir = Path(anchor_dir)
    if sentinel_path(anchor_dir).is_file():
        if not is_anchor_dir(anchor_dir):
            # the likeliest typo: the run root named where the anchor goes; state the anchor's
            # default place
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

    Five wrong answers each get one refusal; none degrades to a skip. The sixth answer is the
    archive (ss12, DL-144): the segment is gone and a receipt licensed the deletion. Without a
    receipt the same disk is loss and refuses."""
    # one spelling of "the same root" (DL-145): readers compare through `normalized_root`
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
    # the receipt is proved whether or not the segment survives: DL-144 makes an archive
    # irreversible, and an unproved receipt is not reportable. `verify_archive_receipt` is the
    # one door (attest.py): sentinel estate, sidecar, PR-02e's consumer rule over the
    # attestation, the digest and chain the receipt names, and that the list names an excused
    # file
    from dsl41.attest import verify_archive_receipt

    segment = wal_path(root, period_id)
    try:
        receipt = verify_archive_receipt(
            # `licensing` is redundant while ss12a's two shapes both name period N's segment;
            # it keeps the walk correct if that rule relaxes
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
    """What taking possession of a root yields: the owner and the sentinel."""

    estate_id: str
    sentinel: Sentinel
    resumed: bool


def default_anchor_dir(run_root: Path) -> Path:
    """`<run-root>.anchor`, a sibling of the root and never inside it.

    ss1.1 names no anchor path. This default is deterministic from the root, so a restart with
    the same `--run-root` reaches the same lineage, and lies outside the archived directory. An
    explicit `--estate-anchor` overrides it."""
    return run_root.parent / f"{run_root.name}.anchor"


def check_root_unused(run_root: Path) -> None:
    """ss1.1's unused-root predicate over the whole estate surface, not the sentinel alone.

    A root keeping a WAL, a seal, a committed period or a populated runs/ is somebody's work
    even without its journal.jsonl. What the launcher pre-stages is excluded: `catalogs/` and
    `periods/.staging/` (stage_next_period).

    It is read-only so the CLI can prove a root claimable before staging a bundle into it or
    starting a supervisor against it."""
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
    """ss1.1's sentinel step, under its ownership rule. The caller holds `leader.lock`, which
    excludes a concurrent opener.

    It refuses a target that already holds any `journal.jsonl` unless it is this estate's
    sentinel for this very claim, left by our own crash (PR-01c). A physical roll passes the
    `claim_id` that first opened the root; an in-place opener passes none, and the sentinel
    proves only that this estate owns the root."""
    existing = read_sentinel(run_root)
    occupied = existing is None and sentinel_path(run_root).exists()
    if occupied:
        # D5, DL-138: this reader does not route through `read_journal`, so it carries its own
        # tombstone, for `header` only. A retired opening says the root predates a retirement;
        # any other file opening was never a journal and falls to the generic refusal below.
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
    """Make `wal/` durable and return the segment's path."""
    directory = run_root / WAL_DIR
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fsync_dir(directory)
    return wal_path(run_root, segment_no)


# -------------------------------------------------------------- staging


class Candidate(BaseModel):
    """ss7's `candidate.json`, beside `staged_manifest.json`.

    The rename to `periods/N+1/` drops the digest from the path; the candidate binds the
    request's staged fields one by one so a retry is told which differed.

    `strict=True` (DL-168) gives the wire's types on a lax `model_validate`, beside
    `_read_artifact`'s strict parse; nested `next_period` uses `StagedNextPeriod`'s own
    strictness."""

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
    """Materialize period 1's inputs and pin its identity (period-model ss1.1, DL-130; DL-66).

    `catalogs/<source_bundle_hash>/` holds the post-placeholder source, byte-exact (F1),
    beside `sources.json`, addressed by those bytes, so a relaunch reuses it. The engine
    installs the committed `periods/000001/manifest.json` at genesis, because `baseline_id`
    and `first_index` are its to know.

    Original paths are recorded because `catalog_hash` covers `SourceSpan.file`. Relocation-
    independent hashing is deliberately not built: it would orphan every journal's resume
    gate."""
    sources = [SourceFile(path=jf.file, text=render_preserve(jf)) for jf in parsed]
    return stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=profile,
        state_machine_version=STATE_MACHINE_VERSION,
    )


def staged_next_from(staged_manifest: StagedManifest) -> StagedNextPeriod:
    """`StagedManifest` -> the client-proposal half, derived from the model's fields
    (DL-137)."""
    return StagedNextPeriod(
        **{name: getattr(staged_manifest, name) for name in StagedNextPeriod.model_fields}
    )


def stage_next_period(
    run_root: Path, *, staged_manifest: StagedManifest, crash_point: CrashPoint = no_crash
) -> StagedNextPeriod:
    """ss7's staging, second step: `staged_manifest.json` and `candidate.json` under
    `periods/.staging/<stage_digest>/`.

    The bundle is the first step and is `period.write_bundle`'s: two sibling destinations
    cannot be renamed in at once, and a repeat is idempotent. `period_id`, `segment_no`,
    `baseline_id`, `clock_domain` and `first_index` are derived at the boundary and excluded
    from `stage_digest` (`StagedNextPeriod`), so a retry closing at another index stages the
    same identity."""
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

    The rename to `periods/N+1/` moves them out of `periods/.staging/`; the install keeps
    `staged_manifest.json` and `candidate.json` so a retry after an install-before-seal crash
    validates the same bytes (ss7). The installed directory counts only when its candidate
    names this digest."""
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


_ArtifactModel = TypeVar("_ArtifactModel", bound=BaseModel)


def _read_artifact(path: Path, model: type[_ArtifactModel]) -> _ArtifactModel | None:
    """Read and validate one closed artifact, or None when this path holds none.

    The read side requires `artifact_format_version` on the wire, so a construction default
    never fills it in (DL-157). Construction parses `raw` again, strict JSON (DL-168):
    `model_validate` on the decoded dict would coerce a wire `true` to `1`, and a call-time
    `strict=True` cascades into nested fields where a model's config does not.

    `decode(raw)` runs first because pydantic's JSON parser accepts a duplicate key where
    `decode` refuses it (PR-12), so the version check and the validation see the same
    payload."""
    raw = read_or_none(path)
    if raw is None:
        return None
    try:
        payload = decode(raw)
        if not isinstance(payload, dict):
            raise BoundaryRefusal(f"{path}: not a JSON object")
        require_artifact_version(payload)
        if issubclass(model, StagedManifest):
            # the committed manifest's completeness rule applies at staged ingress too (DL-252)
            require_manifest_fields(payload, model, where=str(path))
        return model.model_validate_json(raw, strict=True)
    except (CanonError, ValidationError) as exc:
        raise BoundaryRefusal(
            f"{path}: not a {model.__name__} this binary can read ({exc})"
        ) from exc


def load_bundle_catalog(
    run_root: Path, source_bundle_hash: str, *, permit_unknown: bool = False
) -> CatalogIR:
    """Any period's catalog, loaded from the immutable bundle this root holds: the one loader
    (DL-137), shared by staging validation and audit. It parses under the original paths
    `sources.json` records, because the hash covers spans."""
    sources: Sequence[SourceFile] = bundle_sources(run_root, source_bundle_hash)
    try:
        return lower_catalog(
            [parse(source.text, file=source.path) for source in sources],
            permit_unknown=permit_unknown,
        )
    except (JilParseError, LoweringError) as exc:
        raise BoundaryRefusal(
            f"{run_root}: bundle {source_bundle_hash} does not load ({exc}):"
            " a catalog that cannot be rebuilt from its own bundle cannot be"
            " validated or audited (period-model ss7)",
        ) from exc


def load_staged_catalog(
    run_root: Path, staged: StagedManifest, *, permit_unknown: bool = False
) -> CatalogIR:
    """C2, loaded from exactly the staged bytes `stage_digest` names (ss7 phase 1), under the
    original paths `sources.json` records, so it hashes back to the pin on a host without the
    original files."""
    return load_bundle_catalog(run_root, staged.source_bundle_hash, permit_unknown=permit_unknown)


# ---------------------------------------------------------- the ss9 gate


def externally_requested_attempts(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every externally requested attempt in this segment that reached a durable decision (ss9).

    An attempt, not a mutation: the exact-retry promise covers a journaled rejection and an
    applied no-op, so a recent `rejected` CAS loser holds the gate as an applied `STARTJOB`
    would. External means `expect is not None` (the ss6 mandate an engine-side injection never
    carries); `host` commands count by the same test."""
    decided = {
        record["index"]
        for record in records
        if record.get("rec") == "decision" and isinstance(record.get("index"), int)
    }
    # an attempt carries its number under `seq` and a decision under `index`, which shares the
    # attempt's (DL-89); the wrong key empties the join and the horizon protects nothing
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

    Observed age >= horizon, or no externally requested attempt with a durable decision (age =
    infinity): the gate passes and `forced_gate` is null whatever `force_seal` says, so an
    unnecessary `--force-seal` engages nothing (PR-30). Below the horizon, `force_seal: false`
    refuses and `force_seal: true` commits with the gate's output recorded.

    The caller reads the horizon from the closing period's committed manifest (PR-47e); the
    staged C2 profile has no say."""
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
            " Wait it out, or seal with --force-seal (period-model ss9)",
            code="seal_retry_horizon",
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
    """ss2.2: the request's fingerprint over the complete envelope.

    Requests differing only in `force_seal` or actor collide: force is an authorization and the
    actor is attribution, and neither may be swapped under a retry."""
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
    """Every fact ss7 phase 1 reads, and nothing else; phase 1 is pure over it."""

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
    """ss7 phase 1, at readiness, before the barrier. No seal, no T.

    A failure refuses while C1 is still open and correct, so no committed boundary is left
    unopenable."""
    staged, bytes_ = ctx.staged, ctx.staged_bytes
    for name, value in (
        ("the candidate", staged.artifact_format_version),
        ("the staged manifest", bytes_.artifact_format_version),
    ):
        try:
            check_artifact_version({"artifact_format_version": value})
        except CanonError:
            raise BoundaryRefusal(
                f"{name} carries artifact_format_version {value}: this binary implements"
                f" {ARTIFACT_FORMAT_VERSION} (period-model ss8, PR-08d)",
            ) from None
    disagree = [
        f"{field}: candidate {named!r} vs staged bytes {stored!r}"
        for field, named, stored in disagreements(staged, bytes_, StagedNextPeriod.model_fields)
    ]
    if disagree:
        raise BoundaryRefusal(
            f"the request names a candidate the staged bytes do not describe"
            f" ({'; '.join(disagree)}): the engine validates exactly the staged"
            " bytes the fingerprint names (period-model ss7)",
        )
    # the version before the hash: the catalog hash covers `ir_version`, so an older build's
    # candidate would read as a tampered bundle (DL-253)
    if staged.state_machine_version != ctx.state_machine_version:
        raise BoundaryRefusal(
            f"the candidate names state_machine_version {staged.state_machine_version} and"
            f" this period runs v{ctx.state_machine_version}: one executable implements one"
            " version, so an SM bump is a full drain and a new estate, never a transition"
            " (period-model ss2.1, PR-17)",
        )
    recomputed = catalog_hash_v2(ctx.c2)
    if recomputed != staged.catalog_hash:
        raise BoundaryRefusal(
            f"the staged bundle hashes to {recomputed} and the candidate pins"
            f" {staged.catalog_hash}: the boundary would open a catalog it did not"
            " validate (period-model ss7 phase 1)",
        )
    profile_hash = runtime_hash(bytes_.runtime_profile)
    if profile_hash != staged.runtime_hash:
        raise BoundaryRefusal(
            f"the staged runtime profile hashes to {profile_hash} and the candidate pins"
            f" {staged.runtime_hash}: a tampered profile beside the original hash would"
            " pass every shared-field comparison (period-model ss7 phase 1)",
        )
    errors = preflight_errors(ctx.c2, bytes_.runtime_profile, at=ctx.at)
    if errors:
        raise BoundaryRefusal(
            f"the staged estate does not pass preflight ({'; '.join(errors)}):"
            " refuse loudly, run honestly (runner-design ss8)",
        )
    _check_request_id(ctx)
    verdict = classify(
        closing=ctx.c1,
        opening=Baseline(catalog=ctx.c2, profile=bytes_.runtime_profile),
        carried=ctx.carried_state,
    )
    if verdict.refused:
        raise BoundaryRefusal(
            f"the classification refuses the boundary ({', '.join(verdict.refused)}):"
            " a period never opens over live work whose closure changed"
            " (period-model ss10.1)",
        )
    return verdict


def preflight_errors(catalog: CatalogIR, profile: RuntimeProfile, *, at: datetime) -> list[str]:
    """ERROR-severity preflight codes over `catalog`/`profile`, one string per finding
    (`code` or `code (job)`).

    `validate_staged` (period-model ss10.1) and the offline sealer
    (`cli_estate._offline_seal`, DL-240) share it."""
    from dsl41.runner_preflight import preflight

    return [
        f"{item.code}{'' if item.job is None else f' ({item.job})'}"
        for item in preflight(
            catalog,
            machine_policy=profile.machine_policy,
            as_machine=frozenset(profile.as_machine),
            start=at,
            default_tz=profile.default_tz,
            # the profile cannot spell "no map": an empty table would retire SEM-35's
            # unique-city rung everywhere (DL-151/DL-163), so `tz_aliases_of` maps it to None
            tz_aliases=tz_aliases_of(profile),
            semantics=switches_of(profile),
        )
        if item.severity == "ERROR"
    ]


def _check_request_id(ctx: StagedContext) -> None:
    """ss2.2: `request_id` collides across the whole period, not only seal to seal.

    One `request_id`, one command (`control-protocol.md` ss3, PR-30c)."""
    try:
        prior = ctx.decision_index.lookup(ctx.boundary_request.request_id, ctx.request_fingerprint)
    except RequestCollision as exc:
        # an id already spent on a different command; re-raised in this rule's words
        raise BoundaryRefusal(
            f"request_id {ctx.boundary_request.request_id} already names another command"
            f" in this period ({exc}): one request_id, one command -- an ordinary"
            " STARTJOB and a seal cannot both name authoritative decisions"
            " (control-protocol ss3, PR-30c)",
        ) from exc
    # unreachable: a seal never enters the DecisionIndex, which stores
    # `runner_admission.fingerprint` values, and `lookup` returns a prior only on equal
    # fingerprints; this arm needs a sha256 collision
    if prior is not None:  # pragma: no cover
        raise BoundaryRefusal(
            f"request_id {ctx.boundary_request.request_id} already decided at index"
            f" {prior.index} in this period: a seal is a command like any other"
            " (control-protocol ss3, PR-30c)",
        )


@dataclass(frozen=True)
class BoundaryContext:
    """ss7 phase 2's context: phase 1's facts plus what the cutoff produced.

    It holds no candidate sidecar or record: the sidecar's `classification` is phase 2's
    output, so `validate_boundary` runs first and returns it, and `check_candidate` reads the
    document built from it."""

    staged: StagedContext
    committed: CommittedNextPeriod
    committed_manifest: Manifest
    at: datetime
    post_barrier_state: CarriedState


def validate_boundary(ctx: BoundaryContext) -> Classification:
    """ss7 phase 2's first half, after ss6 step 6 and before the sidecar.

    The classifier runs again and its output is the committed classification. The barrier's
    admissions and reconciliation injections may have created executions or latent intent that
    phase 1 never saw; committing the stale phase-1 map would omit an A assumption and fail
    audit (PR-28a)."""
    if not (ctx.at == ctx.post_barrier_state.now):
        raise BoundaryRefusal(
            f"the carried state is not at the cutoff ({ctx.post_barrier_state.now} vs"
            f" T {ctx.at}): C1 owns every tick <= T (period-model ss6)",
        )
    verdict = classify(
        closing=ctx.staged.c1,
        opening=Baseline(catalog=ctx.staged.c2, profile=ctx.committed_manifest.runtime_profile),
        carried=ctx.post_barrier_state,
    )
    if verdict.refused:
        raise BoundaryRefusal(
            f"the post-barrier classification refuses the boundary"
            f" ({', '.join(verdict.refused)}): the cutoff's own admissions created live"
            " work whose closure C2 changes (period-model ss7 phase 2, PR-28a)",
        )
    return verdict


def check_candidate(ctx: BoundaryContext, *, sidecar: Seal, record: Mapping[str, Any]) -> None:
    """ss7 phase 2's second half: the checks that read the candidate document, over in-memory
    models. A failure refuses the commit and the caller's `abort_boundary` restores
    admission."""
    if ctx.committed.first_index != sidecar.closes_at_index + 1:
        raise BoundaryRefusal(
            f"the opening's first_index {ctx.committed.first_index} is not"
            f" closes_at_index + 1 ({sidecar.closes_at_index + 1}): I2 makes every index"
            " estate-monotone (period-model ss3.4, PR-05b)",
        )
    check_seal_record(record)
    disagree = _record_disagreements(sidecar, record)
    if disagree:
        raise BoundaryRefusal(
            f"the `seal` record disagrees with the sidecar it names"
            f" ({'; '.join(disagree)}): recovery selects the sidecar by these"
            " fields and refuses a wrong one (period-model ss2.2)",
        )
    if not (sidecar.state.now == sidecar.scheduler_admitted_through == ctx.at):
        raise BoundaryRefusal(
            f"the snapshot is not at the cutoff (now {sidecar.state.now.isoformat()},"
            f" admitted through {sidecar.scheduler_admitted_through.isoformat()},"
            f" T {ctx.at.isoformat()}): C1 owns every tick <= T and C2 every tick after"
            " it (period-model ss6)",
        )
    # phase 3's load over the in-memory candidates, so the boundary is openable before it commits
    open_from_seal(sidecar, expected_digest=sidecar.digest, manifest=ctx.committed_manifest)


def carried_outbox(
    opened: OpenedRuntime | None, *, at: datetime, machines: Executor | None = None
) -> Outbox:
    """The intents and applied bindings a period opens holding (ss3.5).

    `outbox_pending` is not the whole carry: an applied SPAWN for a live run is not pending.
    Every `bound` and `fw_watch` entry reconstructs its effect and its applied resolution, so
    the preflight identity gates hold the world to the sealed run_id.

    It is built before the segment's own records are read: `Outbox.resolve` refuses an outcome
    for an effect it never saw. `machines` is the executor its effects run on (the resuming
    engine's, or the outbox's own offline). A statechart error out of a carried record names
    that record."""
    outbox = Outbox(machines)
    if opened is None:
        return outbox
    for effect in opened.outbox_pending:
        with _carried(f"the carried intent of effect {effect.effect_id}"):
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
        with _carried(f"the carried execution of effect {entry.effect_id}"):
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


@contextlib.contextmanager
def _carried(what: str) -> Iterator[None]:
    """Add the carried record `what` to a statechart error's report (statechart-engine ss5)."""
    try:
        yield
    except StatechartError as exc:
        exc.facts.append(("carried input", what))
        raise


# ------------------------------------------------------- the seal record

#: ss2.2's `seal` record: the fields recovery selects the sidecar by, plus the request half
#: that makes a live seal answerable.
_SEAL_SCHEMA: Final[dict[str, Any]] = {
    "estate_id": lambda v: isinstance(v, str) and bool(v),
    "period_id": lambda v: is_wire_int(v) and v >= 1,
    "closes_at_index": lambda v: is_wire_int(v) and v >= 0,
    "at": lambda v: isinstance(v, str),
    "digest": is_hash_address,
    "next_period_id": lambda v: is_wire_int(v) and v >= 2,
    "next_baseline_id": is_hash_address,
    # the current recipe, not merely a readable one: the `seal` record is a closed artifact,
    # so this is not `period.check_catalog_hash_version` (D4, DL-138)
    "catalog_hash_version": lambda v: is_wire_int(v) and v == CATALOG_HASH_VERSION,
    "source": lambda v: v == "request",
    "request_id": lambda v: isinstance(v, str) and bool(v),
    "request_fingerprint": lambda v: isinstance(v, str) and bool(v),
    "claimed_actor": lambda v: isinstance(v, str),
    "force_seal": lambda v: isinstance(v, bool),
}


def seal_record(seal: Seal) -> dict[str, Any]:
    """ss2.2's record, the boundary's decision.

    An ordinary `decision` record cannot decide a `seal` request: before the seal a crash
    leaves a durable "applied" for a boundary that never happened, after it records are
    forbidden, and none leaves a lost response unretryable. The record carries the request
    identity, and an exact retry is answered from the committed seal in the new period, ahead of
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
    """A `seal` is a ss2.2 seal: every field present with its exact type, no unknown field."""
    if record.get("rec") != "seal":
        raise EngineError(f"not a seal record: rec is {record.get('rec')!r}")
    check_record_fields(record, _SEAL_SCHEMA, where="seal record", cite="period-model ss2.2")


def _record_disagreements(seal: Seal, record: Mapping[str, Any]) -> list[str]:
    """The fields the ss2.2 record duplicates, compared against the sidecar it names.

    It is derived (DL-145; DL-137's third defect): the record the seal would write against the
    record on disk, over `_SEAL_SCHEMA`'s keys, so a new key is compared automatically. The
    seal's own derivation is the left side of `sidecar X vs record Y`."""
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

    An applied SPAWN for a live run is not pending, so the RUNNING row alone would lose
    `run_id`, `executor_id`, `generation` and the spool binding. Three kinds come from three
    pieces of evidence: the effect for a `pending_spawn`, `spawn.json` for a `bound` run, and
    the first `watch_seq` lines of `watch.jsonl` for an `fw_watch`.

    An applied SPAWN with neither file is the state ss8 forbids (the sealer waits it out,
    PR-27) and refuses here."""
    from dsl41.runner_adapters import WATCH_LOG, load_json, read_watch_log, spool_names_run

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
        # `live_spawns` yields only `pending` and `applied`, and `pending` continued above
        if state != "applied":  # pragma: no cover
            continue
        run_dir = run_root / "runs" / f"{effect.job}.{effect.run_number}"
        run_id = _require_run_id(effect)
        if (run_dir / WATCH_LOG).exists():
            # audit passes the per-run positional prefix: a later period's polls in the same file
            # are its evidence and an instant is not a unique log position (ss3.5), so the cut
            # is a count. The caller establishes its authority (attest.py: `opens_from_seal`
            # pins the sidecar); a log with no claimed entry refuses
            bound = (
                None if watch_prefix is None else watch_prefix.get((effect.job, effect.run_number))
            )
            if watch_prefix is not None and bound is None:
                raise BoundaryRefusal(
                    f"{effect.job}.{effect.run_number}: watch.jsonl exists but the seal"
                    " being re-derived carries no fw_watch entry for this run -- the"
                    " evidence and the claim disagree (period-model ss11)",
                )
            log = read_watch_log(run_dir, prefix=bound)
            assert log is not None  # the file exists and the fold refuses a bad one
            if log.run_id != run_id:
                # DL-118 at the seal: a log naming another run is a stranger's
                raise BoundaryRefusal(
                    f"{effect.job}.{effect.run_number}: watch.jsonl names run_id"
                    f" {log.run_id!r} but the bound run is {run_id!r} -- refusing to"
                    " seal a stranger's watch (DL-118)",
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
            raise BoundaryRefusal(
                f"{effect.job}.{effect.run_number}: an applied SPAWN with no spawn.json"
                " and no watch.jsonl -- ss8 requires every applied CMD SPAWN to be bound"
                " or terminal before the seal commits, and the sealer waits (PR-27)",
            )
        if not spool_names_run(spawn, run_id=run_id):
            # DL-118 as for the watch log: existence is not identity (DL-178y)
            raise BoundaryRefusal(
                f"{effect.job}.{effect.run_number}: spawn.json reports run_id"
                f" {spawn.get('run_id')!r} but the bound run is {run_id!r} -- refusing"
                " to seal a stranger's binding (DL-118)",
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

    Live means the effect's run is the row's current run and the row is STARTING or RUNNING."""
    out: list[tuple[Effect, str]] = []
    for effect in outbox.effects():
        if effect.kind != "SPAWN":
            continue
        row = rows.get(effect.job)
        if row is None or row.run_number != effect.run_number or row.status not in LIVE:
            continue
        state = outbox.state_of(effect.effect_id)
        if state in ("pending", "applied"):
            out.append((effect, state))
    return out


def executing_jobs(outbox: Outbox, rows: Mapping[str, JobRuntime]) -> dict[str, str]:
    """Which jobs have a live execution, `pending` or `applied`, from the WAL alone (ss10.1's
    executing tier).

    `pending_spawn`, `bound` and `fw_watch` all classify EXECUTING (no ss10 rule separates them), and readiness runs before
    the sealer waits out an unbound SPAWN (ss8, PR-27), so the classifier needs no spool
    evidence. The seal artifact does, in `executions_at`."""
    return {effect.job: state for effect, state in live_spawns(outbox, rows)}


def _require_run_id(effect: Effect) -> str:
    if effect.run_id is None:
        raise BoundaryRefusal(
            f"{effect.effect_id}: a SPAWN with no run_id -- a run root written before"
            " DL-118 cannot be sealed, because the boundary carries the binding the"
            " effect was supposed to mint (period-model ss2.3, PR-36a)",
        )
    return effect.run_id


# ----------------------------------------------------------- the writes


@dataclass(frozen=True)
class Snapshot:
    """What the cutoff barrier hands the boundary: the estate at T, taken after ss6 step 6 and
    before any sidecar byte, in the single-writer loop."""

    state: SealedState
    carried: CarriedState
    outbox_pending: tuple[Effect, ...]
    executions: tuple[Execution, ...]
    closes_at_index: int
    at: datetime
    epoch: int


@dataclass(frozen=True)
class CommittedBoundary:
    """A committed boundary: the three artifacts that now name each other. `record` is derived
    from the seal (DL-137)."""

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
    """ss6 steps 7-8 and ss3's three writes, in the order that is the durability argument.

    The caller has frozen admission, drained to T, parked every FW task and settled the outbox.
    Every non-commit exit before the `seal` append raises, and the caller runs
    `abort_boundary`, because its flag closed admission."""
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
    # the committed manifest is written into the staged directory, which is renamed whole, so
    # the boundary names the artifacts it validated (ss7)
    plan = _prepare_install(
        run_root, staged=staged, committed_manifest=committed_manifest, crash_point=crash_point
    )
    check_candidate(context, sidecar=sidecar, record=record)
    if plan.staging is not None:
        # quarantine runs here, not while planning: moving the old candidate is the one
        # destructive act in the reversible interval before the `seal` append
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
    # ---- the point of no return: once any of these bytes may have been written, every failure
    # is a fail-stop with an unknown outcome, never an abort (PR-28d).
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
    except StatechartError:
        # a gap in the subscription chart (the `seal` fan-out) or the anchor charts (the close
        # CAS, DL-336) stops the engine with exit 7 and the record durable; resume reads the
        # lineage (statechart-engine ss5)
        raise
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
    """The install step, decided before phase 2 and acted on after it: the directory to rename
    in (None on the reuse path) and the installed candidate it supersedes."""

    staging: Path | None
    supersedes: Candidate | None


def _prepare_install(
    run_root: Path,
    *,
    staged: StagedNextPeriod,
    committed_manifest: Manifest,
    crash_point: CrashPoint,
) -> _Install:
    """Put the committed manifest where the boundary will name it and plan the install,
    without the destructive act (ss7).

    The rename to `periods/N+1/` drops the digest from the path, so an installed candidate
    carries its own identity (`candidate.json`). A retry with an equal `stage_digest` reuses it
    and regenerates `manifest.json` from its own cutoff (`first_index` is attempt output); one
    with a different digest quarantines the installed candidate and installs its own (PR-30d)."""
    target = period_dir(run_root, committed_manifest.period_id)
    installed = read_candidate(target)
    if installed is not None and installed.stage_digest == staged.stage_digest:
        # the reuse path has its own liturgy; the fresh path's fsyncs do not apply (PR-30d,
        # PR-30g)
        write_period_manifest(run_root, committed_manifest)
        crash_point("after_committed_manifest")
        return _Install(staging=None, supersedes=None)
    if installed is None and target.exists():
        raise BoundaryRefusal(
            f"{target} exists and carries no candidate.json: the boundary will not"
            " blindly reuse a period directory it cannot identify (period-model ss7)",
        )
    staging = staging_dir(run_root, staged.stage_digest)
    if read_candidate(staging) is None:
        # a second client or a retention sweep removed the staged directory after readiness
        raise BoundaryRefusal(
            f"{staging}: no staged candidate at this digest -- the request names bytes"
            " that were never staged (period-model ss7)",
        )
    durable_write(
        str(staging / "manifest.json"),
        canonical_bytes(committed_manifest.model_dump(mode="json")) + b"\n",
    )
    crash_point("after_committed_manifest")
    return _Install(staging=staging, supersedes=installed)


def _quarantine(run_root: Path, target: Path, installed: Candidate) -> None:
    """Move an outdated installed candidate to a path that survives alternating candidates
    (S1, S2, S1) and is idempotent for the same bytes (ss7, PR-30d)."""
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
    """sha256 over stored bytes, not over a model's canonical form."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class OpenedPeriod:
    """The opening half of a boundary: the new segment and what seeds the engine."""

    opened: OpenedRuntime
    journal: Journal
    #: None when this root re-opens a period whose head already moved: the claim was consumed
    #: and only the segment was missing (ss11)
    claim: Claim | None


def check_opening_version(opening: CommittedNextPeriod, *, where: str) -> None:
    """A committed boundary opens under the state-machine version it names, and only a build
    deriving that version may open it (period-model ss2.1, PR-17).

    Every opener asks this first, before it claims, writes or imports anything (DL-253): the
    catalog hash covers `ir_version`, so a later hash gate would refuse an older build's
    boundary after the claim and sentinel were written."""
    if opening.state_machine_version != STATE_MACHINE_VERSION:
        raise EngineError(
            f"{where}: the committed boundary opens period {opening.period_id} under"
            f" state_machine_version {opening.state_machine_version} and this build derives"
            f" v{STATE_MACHINE_VERSION}: one executable implements one version, so an SM"
            " bump is a full drain and a new estate, never a transition"
            " (period-model ss2.1, PR-17)"
        )


def open_next_period(
    *,
    run_root: Path,
    anchor: EstateAnchor,
    committed: CommittedBoundary,
    catalog: CatalogIR | Callable[[], CatalogIR],
    lock: Proof | None = None,
    crash_point: CrashPoint = no_crash,
) -> OpenedPeriod:
    """ss7's opening half: claim the successor, write the opening segment, move the head, and
    seed from the seal.

    `at` on the opening segment is T, the seal's cutoff instant, and every non-derived field
    comes from the seal's `next_period`, so two openings of one seal are byte-identical
    (PR-07).

    A callable `catalog` is resolved after the claim and before the segment, where ss7 puts a
    physical roll's import.

    Idempotent at every step: the claim resumes on its `claim_id`, an existing segment is
    verified rather than appended to, and `claimed -> open` is a no-op once performed."""
    seal = committed.seal
    opening = seal.next_period
    check_opening_version(opening, where=str(run_root))
    stored = anchor.require(seal.estate_id)
    head = stored.head
    # ss11's never-opened row: the head says this root opened the period and only the segment
    # is missing. The claim was consumed, so the byte-identical segment is rewritten (PR-07).
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
    # ss1.3: a break-glass override is recorded in the anchor and copied into the next
    # `segment`
    forced = pending_reclaim(stored, opening.period_id)
    path = open_wal(run_root, opening.segment_no)
    existing = path.exists()
    if existing:
        _check_existing_segment(path, opening, link, forced)
        # a previous opener may have died before its fsync; the CAS below relies on this file
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
    try:
        if not existing:
            fsync_dir(path.parent)
        crash_point("after_opening_segment")
        if claim is not None:
            anchor.open_claimed(claim_id=claim.claim_id, period_id=opening.period_id, root=run_root)
        crash_point("after_open_cas")
        opened = open_from_seal(seal, expected_digest=seal.digest, manifest=committed.manifest)
    except BaseException:
        # close the descriptor and leave the term (as `detach` does), so the caller's cleanup
        # decides about the lock; a failing flush must not replace the original error
        with contextlib.suppress(OSError, ValueError):
            journal.detach()
        raise
    return OpenedPeriod(opened=opened, journal=journal, claim=claim)


def pending_reclaim(anchor: Anchor, next_period: int) -> Reclaimed | None:
    """The break-glass override that cleared this opening's way, or None: the newest entry for
    this period, since a lineage can be reclaimed more than once."""
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
    """Verify a segment written by an interrupted opener; a second `segment` record for one
    period is the two-candidate state I1 forbids."""
    records = read_journal(path)
    segment = records[0]
    # the derived field set (DL-137), as strong as `check_manifest_against_segment`
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

    A committed seal whose sidecar is missing refuses: the boundary is unrecoverable (ss11)."""
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
    """ss11 step 3's answer: the seal this root's replay starts from, and whether it still has
    to be opened."""

    #: None only in period 1 before any seal exists
    seal: Seal | None
    #: True when the seal is committed and no successor segment exists
    opens_next: bool

    def __post_init__(self) -> None:
        if self.opens_next and self.seal is None:
            # `select_seal` sets opens_next only on the committed-seal branch, which reads a
            # seal first (DL-171)
            raise AssertionError("opens_next requires seal (Lineage invariant, DL-171)")

    def target_period(self, fallback: int) -> int:
        """period-model ss11 step 5: the period a resume opens into: `fallback`, the newest
        segment's period, unless a committed boundary is unopened; then the seal's next
        period."""
        if self.opens_next:
            assert self.seal is not None  # structural: __post_init__ above
            return self.seal.next_period.period_id
        return fallback


def select_seal(run_root: Path, records: Sequence[Mapping[str, Any]]) -> Lineage:
    """ss11 step 3: select the seal by lineage, from what this root holds.

    A rolled root never holds the predecessor's WAL or `seal` record, so the local `segment`
    is the proof. The sidecar's recomputed digest and every duplicated field are verified
    against the naming record; a matching digest proves integrity, never derivation."""
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
    """Every duplicated field of the `seal` record equals the sidecar's (ss2.2/ss11): recovery
    selects the sidecar by them, and audit refuses a rewritten record (DL-137)."""
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
    """ss11 step 4: act on the head before anything is replayed. Each row repairs one crash
    window:

    - `open(N, this root)` with N's `seal` record present performs the CAS the crashed sealer
      did not;
    - `closed` with no following segment leaves the claim to the opener;
    - `claimed` with our claim and a durable segment that agrees with its seal moves the head
      to `open`;
    - `claimed` with another's claim refuses, naming the holder;
    - `open(1, this root)` with a provisional row and a durable segment finalizes.

    The segment must agree with the seal, not merely exist, and the check precedes the CAS, as
    in `open_next_period` (DL-145)."""
    current = anchor.require(estate_id)
    head = current.head
    if (
        lineage.seal is not None
        and lineage.opens_next
        and isinstance(head, OpenHead)
        and head.period_id == lineage.seal.period_id
    ):
        # the seal record landed and the head did not move (PR-02b); the period check keeps
        # this off the neighbouring row, where a head open at the successor is the opener's
        # business. Fsync the closing WAL first: readable is not durable, and a CAS over a line
        # a power cut removes leaves a successor whose naming seal is gone. Any failure here
        # stays fail-stopped.
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
            # one evidence standard for the head move (DL-145): `_check_existing_segment`
            # before the CAS, as in `open_next_period`; a crash between `after_opening_segment`
            # and `after_open_cas` is repaired here (DL-142's check-before-CAS order)
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
            # readable is not durable: make the segment durable before the head names it
            fsync_file(segment)
            return anchor.open_claimed(
                claim_id=claim.claim_id, period_id=claim.next_period, root=run_root
            )
        return current
    if isinstance(head, OpenHead) and wal_path(run_root, head.period_id).exists():
        fsync_file(wal_path(run_root, head.period_id))  # same rule at genesis finalize
        return anchor.finalize(head.period_id)
    return current


# ------------------------------------------- whose root this is (DL-224)

#: The causes and remedies every root-authority refusal states.
_ROOT_REFUSAL_TAIL = (
    " The usual causes are a copy or a restore at another path, a target"
    " whose claim was reclaimed, or a roll that stopped before its claim."
    " Resume refuses such a root; a restore must land at the recorded path,"
    " and a roll that stopped before its claim is finished by running the"
    " opener (`dsl41 run --open-from`) again (period-model ss1.3, DL-224)"
)


class RootAuthorityError(EngineError):
    """ss1.3's resume rule refused this root (DL-224).

    Its own type, so the lock-free pre-check reports this refusal and no other; the locked
    path raises every other error in its own order and words."""


def same_root(recorded: str, here: str) -> bool:
    """Whether a recorded root is the root at `here` (`normalized_root`).

    True when the normalized strings are equal or the recorded path is the same directory
    (`os.path.samefile`): a symlink, case-variant spelling or bind mount is this root; a copy
    is not. A recorded path that cannot be stat'ed is not this root."""
    if normalized_root(recorded) == here:
        return True
    try:
        return os.path.samefile(recorded, here)
    except (OSError, ValueError):
        return False


def newest_opened_period(run_root: Path) -> int | None:
    """The `period_id` of the opening record of this root's newest opened segment, read without
    repair (ss1.3's OWNED half, DL-224).

    A newest segment whose first line is empty or torn never opened; this reads the segment
    before it, as `_drop_never_opened_segment` does when it removes the file. None when no
    opening is readable; `read_journal` refuses that root later."""
    segments = wal_segments(run_root)
    if not segments:
        return None
    first = _first_line(wal_path(run_root, segments[-1]))
    if _never_opened(first) and len(segments) >= 2:
        first = _first_line(wal_path(run_root, segments[-2]))
    try:
        record = json.loads(first)
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or not is_opening(record):
        return None
    period = record.get("period_id")
    return period if isinstance(period, int) and not isinstance(period, bool) else None


def _first_line(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.readline()
    except OSError as exc:
        raise EngineError(f"{path}: unreadable: {exc}") from exc


def _never_opened(first: bytes) -> bool:
    """Empty, or torn: no newline and not a JSON document, the first lines `repair_tail` cuts
    to nothing. A line nested too deep to parse is not a document, as in
    `period._is_period_root_line`."""
    if first.endswith(b"\n"):
        return False
    try:
        json.loads(first)
    except (ValueError, RecursionError):
        return True
    return False


def _recorded_roots(anchor: Anchor) -> list[str]:
    """Every root the anchor records: registry rows, and the head's `root`, `closing_root` or
    `target_root`."""
    head = anchor.head
    recorded = [row.root for row in anchor.periods.values()]
    if isinstance(head, OpenHead):
        recorded.append(head.root)
    elif isinstance(head, ClosedHead):
        recorded.append(head.closing_root)
    else:
        recorded.append(head.target_root)
    return recorded


def require_resume_root(anchor: Anchor, *, anchor_path: Path, run_root: Path) -> None:
    """ss1.3's resume rule (DL-224): the anchor names this root, and this root owns the period
    its newest opened segment holds. Roots compare by `same_root`.

    NAMED is the early filter: a root no row or head names is refused. OWNED is authoritative:
    the registry row for that period names this root or, with no row yet, the head is this
    root's claim. A misplaced restore passes the first and fails the second. Read-only; the
    caller runs it before any repair."""
    here = normalized_root(run_root)
    recorded = _recorded_roots(anchor)
    if not any(same_root(root, here) for root in recorded):
        named = sorted({normalized_root(root) for root in recorded})
        raise RootAuthorityError(
            f"{anchor_path}: this anchor does not name {here} -- its registry and"
            f" head name {', '.join(named)}." + _ROOT_REFUSAL_TAIL
        )
    period = newest_opened_period(run_root)
    if period is None:
        return
    row = anchor.row(period)
    if row is not None:
        if not same_root(row.root, here):
            raise RootAuthorityError(
                f"{anchor_path}: this anchor does not hold period {period} for {here}"
                f" -- its registry row for period {period} names"
                f" {normalized_root(row.root)}." + _ROOT_REFUSAL_TAIL
            )
        return
    head = anchor.head
    if isinstance(head, ClaimedHead) and same_root(head.target_root, here):
        return
    raise RootAuthorityError(
        f"{anchor_path}: this anchor does not hold period {period} for {here} -- no"
        f" registry row names period {period}, and the head is {spell_head(head)}, not a"
        " claim by this root." + _ROOT_REFUSAL_TAIL
    )


def resume_root_refusal(
    run_root: Path, anchor_dir: Path | None, *, locked: bool = False
) -> EngineError | None:
    """`require_resume_root` for a CLI route that resumes, before it stages anything or wires a
    supervisor: the refusal, or None.

    Unlocked (`locked=False`) it reads the sentinel and `anchor.json`, writes nothing, and
    reports only `RootAuthorityError`. Any other error (corrupt sentinel or anchor, stray
    `wal/` entry, unreadable segment, missing anchor, another estate's anchor) passes
    silently; `resume_run` raises each in its own words.

    An unlocked snapshot can be stale, so a caller that gets a refusal holds the run-root lock
    and asks again with `locked=True`. That runs `resume_run`'s steps (sentinel, anchor lock,
    `require`, this rule) and releases the anchor lock. Under the locks every `EngineError`,
    busy lock included, is returned as the refusal; other exceptions propagate."""
    anchor = EstateAnchor(anchor_dir or default_anchor_dir(run_root))
    if locked:
        try:
            sentinel = read_sentinel(run_root)
            if sentinel is None:
                return None  # `resume_run` takes its no-sentinel path
            anchor.acquire()
            try:
                current = anchor.require(sentinel.estate_id)
                require_resume_root(current, anchor_path=anchor.path, run_root=run_root)
            finally:
                anchor.release()
        except EngineError as refused:
            return refused
        return None
    try:
        sentinel = read_sentinel(run_root)
        # read, never acquire: `acquire` would create a missing anchor directory
        stored = None if sentinel is None else anchor.read()
        if sentinel is not None and stored is not None and stored.estate_id == sentinel.estate_id:
            require_resume_root(stored, anchor_path=anchor.path, run_root=run_root)
    except RootAuthorityError as refused:
        return refused
    except Exception:  # noqa: BLE001 -- every other error is the locked path's to raise
        return None
    return None
