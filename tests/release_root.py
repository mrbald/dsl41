"""The run root the release tests' stub engine keeps (tests/release_site.py, DL-412, DL-420).

The stub writes what the real engine writes, with the package's own models and writers: the
sentinel and period 1's segment, a committed seal (its sidecar, the `seal` record, the next
period's manifest and the bundle of the files sealed), and the opening of the next period. So
the release's service side reads it with the readers the engine's resume and `dsl41 journal`
use: the lineage proof, the opening proof, the manifest and bundle readers and `read_journal`.
A seal's state is empty: nothing the release reads depends on the rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dsl41.boundary import read_seal, seal_record
from dsl41.ir import lower_catalog
from dsl41.period import (
    RuntimeProfile,
    Sentinel,
    SourceFile,
    genesis_manifest,
    read_period_manifest,
    read_sentinel,
    seal_path,
    segment_record,
    stage_manifest,
    wal_path,
    write_bundle,
    write_period_manifest,
    write_sentinel,
)
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_journal import read_journal
from dsl41.seal import (
    BoundaryRequest,
    CommittedNextPeriod,
    Seal,
    SealedState,
    StagedNextPeriod,
    baseline_id_for,
)

CLOCK = "real"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")


def genesis(root: Path) -> None:
    """A period-1 root: the sentinel, period 1's manifest and its segment record."""
    estate_id = str(uuid.uuid4())
    write_sentinel(root, Sentinel(estate_id=estate_id))
    manifest = genesis_manifest(
        lower_catalog([]), clock_domain=CLOCK, state_machine_version=STATE_MACHINE_VERSION
    )
    write_period_manifest(root, manifest)
    _append(wal_path(root, 1), segment_record(manifest, estate_id=estate_id, at=_now()))


def seal(
    root: Path,
    closing: int,
    *,
    request_id: str,
    actor: str,
    epoch: int,
    closes_at_index: int,
    files: list[str],
    opened_hash: str | None = None,
) -> dict[str, Any]:
    """Commit the seal of period CLOSING under REQUEST_ID: the bundle of FILES, the next
    period's manifest (naming OPENED_HASH instead of that bundle, when given), the sidecar and
    the `seal` record. Returns the next period's number, baseline and bundle."""
    sentinel = read_sentinel(root)
    assert sentinel is not None
    opening = read_journal(wal_path(root, closing))[0]
    address = write_bundle(
        root, [SourceFile(path=file, text=Path(file).read_text()) for file in files]
    )
    staged = stage_manifest(
        lower_catalog([]),
        source_bundle_hash=address,
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    proposed = StagedNextPeriod(
        **{name: getattr(staged, name) for name in StagedNextPeriod.model_fields}
    )
    period = closing + 1
    baseline = baseline_id_for(
        estate_id=sentinel.estate_id, period_id=period, stage_digest=proposed.stage_digest
    )
    first_index = max(closes_at_index, opening["first_index"] - 1) + 1
    manifest = staged.commit(
        period_id=period,
        baseline_id=baseline,
        clock_domain=CLOCK,
        segment_no=period,
        first_index=first_index,
    )
    if opened_hash is not None:
        manifest = manifest.model_copy(update={"source_bundle_hash": opened_hash})
    write_period_manifest(root, manifest)
    at = _now()
    sidecar = Seal(
        estate_id=sentinel.estate_id,
        period_id=closing,
        baseline_id=opening["baseline_id"],
        catalog_hash=opening["catalog_hash"],
        source_bundle_hash=opening["source_bundle_hash"],
        runtime_hash=opening["runtime_hash"],
        state_machine_version=STATE_MACHINE_VERSION,
        closes_at_index=first_index - 1,
        closed_at=at,
        clock_domain=CLOCK,
        epoch=epoch,
        prev_seal_digest=None if closing == 1 else read_seal(root, closing - 1).digest,
        scheduler_admitted_through=at,
        boundary_request=BoundaryRequest(
            source="request", request_id=request_id, claimed_actor=actor, force_seal=False
        ),
        request_fingerprint="sha256:" + "0" * 64,
        state=SealedState(now=at),
        next_period=CommittedNextPeriod(
            **proposed.model_dump(),
            period_id=period,
            segment_no=period,
            baseline_id=baseline,
            clock_domain=CLOCK,
            first_index=first_index,
        ),
    )
    path = seal_path(root, closing)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(sidecar.to_bytes())
    _append(wal_path(root, closing), seal_record(sidecar))
    return {"period": period, "baseline": baseline, "hash": address, "closing": closing}


def open_next(root: Path, closing: int, epoch: int) -> None:
    """The opening of the period seal CLOSING committed, as the opener writes it: the segment
    record from that period's manifest, then a leader record under EPOCH."""
    sentinel = read_sentinel(root)
    assert sentinel is not None
    sidecar = read_seal(root, closing)
    period = closing + 1
    manifest = read_period_manifest(root, period)
    assert manifest is not None
    link = {"period_id": closing, "digest": sidecar.digest}
    record = segment_record(
        manifest, estate_id=sentinel.estate_id, at=sidecar.closed_at, opens_from_seal=link
    )
    _append(wal_path(root, period), record)
    _append(wal_path(root, period), {"rec": "leader", "epoch": epoch, "at": _now().isoformat()})
