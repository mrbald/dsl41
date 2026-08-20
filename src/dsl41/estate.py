"""Estate-level transactions over roots: the physical roll and adoption.

Normative spec: `docs/period-model.md` ss1.1 (the sentinel and the one
ownership rule), ss1.3 (the successor fence, `adopting`, the physical
roll's attestation gate), ss7 (the two openers) and ss11 (the resume
matrix and the seven-step adoption transaction). Built by DL-134 as U7.
Obligations PR-01c, PR-02a, PR-02d, PR-48 in ss13.

Two operations, one shape. Each takes possession of a root that is not yet
this estate's period, fences it, and hands the ordinary machinery a root it
can resume. Neither is a second semantic path: **the seal and opening
format are identical whether the next period continues in place or opens a
fresh root** (PR-07), and adoption seals period 1 through the COMMON seal
body -- the same staging, the same phase 1, the same phase 2, the same
three writes -- never a private one.

**The physical roll's order is the whole argument** (ss7): new-root
`leader.lock`, sentinel durable, `anchor.lock` and the claim, the import,
the segment, the head. The sentinel goes BEFORE the claim: draft 11 said
claim-first, which let B move the head to `claimed(B)`, die before its
sentinel, and leave a root an old binary treats as unused and geneses
into -- and after a `reclaim` that is a fork. No state may exist in which
the head is `claimed(target_root)` while `target_root` lacks a valid
sentinel (PR-01a).

**Adoption's order is fence first, authority second, on a drained
estate** (ss11). Readiness runs BEFORE the fence, so a C2 that cannot open
refuses while the legacy estate is still the legacy engine's; the fence is
a hard link plus a rename, so there is no instant at which `journal.jsonl`
is absent; and only then does the anchor gain `adopting`, which is what
gives adoption ONE recovery owner -- `run --resume` refuses by name until
period 1 seals.
"""

from __future__ import annotations

import os
import uuid

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dsl41.ast_jil import JilParseError, parse
from dsl41.attest import verify_attestation
from dsl41.boundary import (
    Anchor,
    ClaimedHead,
    ClosedHead,
    check_seal_record,
    CommittedBoundary,
    CrashPoint,
    EstateAnchor,
    SealRequest,
    StagedContext,
    claim_id_for,
    claim_root,
    executing_jobs,
    live_spawns,
    load_staged_catalog,
    OpenHead,
    no_crash,
    normalized_root,
    open_next_period,
    open_wal,
    read_seal,
    seal_record,
    validate_staged,
    check_record_names_sidecar,
    seal_path,
)
from dsl41.canon import canonical_bytes
from dsl41.classify import Baseline, carried_from_oracle
from dsl41.ir import CatalogIR, LoweringError, lower_catalog
from dsl41.oracle import Oracle
from dsl41.period import (
    GENESIS_FIRST_INDEX,
    GENESIS_PERIOD_ID,
    GENESIS_SEGMENT_NO,
    Manifest,
    RuntimeProfile,
    Sentinel,
    SourceFile,
    StagedManifest,
    attestation_path,
    bundle_sources,
    opening_at,
    period_dir,
    read_period_manifest,
    read_sentinel,
    seal_dir,
    segment_record,
    sentinel_path,
    stage_manifest,
    wal_path,
    write_bundle,
    write_period_manifest,
    write_sentinel,
)
from dsl41.runner_adapters import SupervisorConn, WATCH_LOG, fsync_dir, load_json
from dsl41.runner_clock import EngineError
from dsl41.runner_history import stored_input_paths
from dsl41.runner_hosts import LOCAL_EXECUTOR_ID, seed_local_executor
from dsl41.oracle_state import JobRuntime
from dsl41.runner_journal import (
    Replay,
    read_attempts,
    read_journal,
    replay_inputs,
)
from dsl41.runner_ledger import STATE_MACHINE_VERSION, Proof, next_epoch
from dsl41.runner_supervisor import PROTOCOL_VERSION as SUPERVISOR_PROTOCOL_VERSION
from dsl41.runner_procid import durable_write, mkdir_durable, verify_alive
from dsl41.seal import open_from_seal, OpenedRuntime, Seal, StagedNextPeriod


@dataclass(frozen=True)
class Rolled:
    """A physical roll, done: the new root now holds period N+1.

    Deliberately no `Journal`: the roll writes the opening `segment` and
    hands the ROOT to the ordinary resume path, which opens its own. What
    comes back is what the caller can SAY about the roll."""

    #: the seal that was imported and opened from
    seal: Seal
    #: where it came from, so the caller can say so
    closing_root: Path
    #: ss7 phase 3's load over that seal -- the identity the new period
    #: opened under
    opened: OpenedRuntime


def roll_into_root(
    new_root: Path,
    *,
    anchor_dir: Path,
    catalog_of: Callable[[Path, Manifest], CatalogIR],
    lock: Proof | None = None,
    crash_point: CrashPoint = no_crash,
) -> Rolled:
    """ss7's second opener: open `next_period` into a fresh root.

    Reads the lineage head, requires it `closed`, requires the closing
    period fully quiescent (ss8: no live executions at all) **and
    attested** (ss1.3), imports the four artifacts the new root needs, and
    then runs the SAME opening the in-place path runs.

    **Attested, not merely audited-somewhere.** `run --open-from` refuses
    unless `seals/<N>.audit.json` exists in `closing_root` and passes
    `verify` -- a file that merely exists is not enough (PR-02d). Draft 5
    let B import a seal it could never verify and then required it to audit
    C1 with none of C1's inputs.

    The caller holds `leader.lock` on `new_root` already, because starting
    a supervisor or staging bytes into a root is an act on an estate this
    process may turn out not to lead."""
    anchor = EstateAnchor(anchor_dir)
    # read BEFORE the lock: the claim below is a compare-and-swap that
    # re-reads under it and refuses a head that moved, so this read only
    # has to be good enough to compute a claim_id -- and the sentinel must
    # be durable before that claim exists at all (PR-01a)
    stored = anchor.read()
    if stored is None:
        raise EngineError(
            f"{anchor.path}: this lineage has no anchor -- a physical roll opens the"
            " successor of a seal, and there is no lineage here to succeed"
            " (period-model ss1.3)"
        )
    closing_root, period_id, seal_digest = _roll_source(stored, anchor, new_root)
    seal = read_seal(closing_root, period_id)
    if seal.digest != seal_digest:
        raise EngineError(
            f"{seal_path(closing_root, period_id)}: digest {seal.digest} but the"
            f" head says {seal_digest} -- the closing root does not hold the seal"
            " this lineage closed with (period-model ss11)"
        )
    if seal.executions:
        # ss8's mode table: a physical roll while jobs are live is REFUSED.
        # The supervisor is one per run root and a new-root engine cannot
        # reach the old root's work; the bridge that lifts this is a
        # non-goal (ss12)
        live = ", ".join(f"{entry.job}.{entry.run_number}" for entry in seal.executions)
        raise EngineError(
            f"seal {seal.digest} carries live execution(s) ({live}): a physical roll"
            " needs every execution terminal, because the supervisor is one per run"
            " root and a new-root engine cannot reach the old root's work"
            " (period-model ss8)"
        )
    verify_attestation(closing_root, period_id)
    opening = seal.next_period
    manifest = read_period_manifest(closing_root, opening.period_id)
    if manifest is None:
        raise EngineError(
            f"{closing_root}: periods/{opening.period_id:06d}/manifest.json is not"
            " there -- the boundary installed it before the record that names it, so"
            " a roll that cannot find it is rolling from a pruned root"
            " (period-model ss7)"
        )
    claim_id = claim_id_for(
        prev_seal_digest=seal.digest,
        next_period=opening.period_id,
        target_root=new_root,
    )
    # the target may not exist yet, and the sentinel is the first thing
    # written INTO it, so every created component is made durable here --
    # unconditionally, so a retry after a failed fsync repairs it rather
    # than skipping it. `acquire_run_root` does the same for the caller
    # that took the lock; both paths reach the same helper
    mkdir_durable(str(new_root))
    os.chmod(new_root, 0o700)
    claim_root(new_root, estate_id=seal.estate_id, claim_id=claim_id)
    crash_point("after_roll_sentinel")
    anchor.acquire()
    committed = CommittedBoundary(seal=seal, record=seal_record(seal), manifest=manifest)

    def imported() -> CatalogIR:
        # ss7 puts the import BETWEEN the claim and the segment, and it is
        # idempotent by content address: re-importing after a crash writes
        # the same bytes to the same names
        import_boundary(closing_root, new_root, seal=seal, manifest=manifest)
        crash_point("after_import")
        return catalog_of(new_root, manifest)

    try:
        period = open_next_period(
            run_root=new_root,
            anchor=anchor,
            committed=committed,
            catalog=imported,
            lock=lock,
            crash_point=crash_point,
        )
    finally:
        # the roll is a TRANSACTION and it ends here: the head is
        # `open(N+1, new_root)`, the segment that justifies it is durable,
        # and the engine that resumes this root takes the lineage lock for
        # its own process lifetime. Holding on would exclude that engine --
        # `flock` is per open file description, so a second acquire in this
        # same process is refused like anyone else's. Nothing can act in
        # the gap: an `open` head refuses genesis, a claim, a second roll
        # and a reclaim alike, and `leader.lock` on `new_root` is held
        # throughout.
        anchor.release()
    period.journal.detach()  # the descriptor, not the term: the resume opens its own
    return Rolled(seal=seal, closing_root=closing_root, opened=period.opened)


def _roll_source(stored: Anchor, anchor: EstateAnchor, new_root: Path) -> tuple[Path, int, str]:
    """Which seal this roll opens, and where it lives: `(closing_root,
    closing period, seal digest)`.

    Two heads answer it, because a roll is a transaction that can be
    interrupted. `closed` is the ordinary start. `claimed` BY THIS ROOT is
    our own claim resumed -- ss1.3 makes the claim idempotent on
    `claim_id`, so a crash after the claim and before the segment re-runs
    here rather than needing break-glass -- and the claim FILE carries the
    seal it was taken against, which is why the head does not have to.
    Everything else refuses, and the completed case refuses by naming the
    verb that continues it."""
    head = stored.head
    if isinstance(head, ClosedHead):
        return Path(head.closing_root), head.period_id, head.seal_digest
    if isinstance(head, OpenHead) and normalized_root(head.root) == normalized_root(new_root):
        raise EngineError(
            f"{anchor.path}: period {head.period_id} is already open in {new_root}:"
            " a completed roll is reopened with `dsl41 run --resume --estate-anchor"
            f" {anchor.dir}`, not rolled again (period-model ss7)"
        )
    if isinstance(head, ClaimedHead) and normalized_root(head.target_root) == normalized_root(
        new_root
    ):
        claim = anchor.read_claim(head.claim_id)
        if claim is None:
            raise EngineError(
                f"{anchor.path}: the head names claim {head.claim_id} and"
                f" {anchor.claim_path(head.claim_id)} is not there -- the claim file is"
                " written before the head moves, so this state is unreachable without"
                " something deleting it (period-model ss1.3)"
            )
        closing = claim.next_period - 1
        row = stored.row(closing)
        if row is None:
            raise EngineError(
                f"{anchor.path}: period {closing} has no registry row -- a resumed"
                " claim finds its closing root through the registry (period-model ss1.3)"
            )
        return Path(row.root), closing, claim.prev_seal_digest
    raise EngineError(
        f"{anchor.path}: the head is {head.state} and this root does not hold it:"
        " a physical roll opens the period a committed seal left unopened, or resumes"
        " its own claim, and nothing else (period-model ss7)"
    )


def import_boundary(source: Path, target: Path, *, seal: Seal, manifest: Manifest) -> None:
    """ss1.3's import: the four artifacts a rolled root needs to be
    resumable on its own.

    `seals/<N>.json`, `seals/<N>.audit.json`, `catalogs/<C2 bundle>/` and
    `periods/<N+1>/`. Idempotent by content address -- the bundle is
    re-materialized from its own recorded bytes and the three files are
    written by the liturgy over whatever a crashed import left -- so a
    re-import after a crash is a no-op with the same bytes (ss11's matrix
    row).

    C1's WAL and C1's own bundle are deliberately NOT imported. Re-deriving
    C1 in the new root would need C1's whole proof set, and importing that
    on every roll is retention policy, not a boundary mechanism -- the
    attestation is what carries the proof, and the anchor registry is how a
    caller who retained C1 finds it (ss1.3)."""
    period_id, opening = seal.period_id, seal.next_period
    write_bundle(target, bundle_sources(source, opening.source_bundle_hash))
    mkdir_durable(str(seal_dir(target)))
    os.chmod(seal_dir(target), 0o700)
    durable_write(str(seal_path(target, period_id)), seal_path(source, period_id).read_bytes())
    durable_write(
        str(attestation_path(target, period_id)),
        attestation_path(source, period_id).read_bytes(),
    )
    destination = period_dir(target, opening.period_id)
    mkdir_durable(str(destination))
    os.chmod(destination, 0o700)
    for entry in sorted(period_dir(source, opening.period_id).iterdir()):
        if entry.is_file():
            durable_write(str(destination / entry.name), entry.read_bytes())
    installed = read_period_manifest(target, opening.period_id)
    if installed is None or installed != manifest:
        # the import is what the new period opens under, so an import that
        # produced something other than the boundary's own manifest is a
        # copy nobody validated -- refused before a segment names it
        raise EngineError(
            f"{destination}: the imported manifest is not the one the boundary"
            " committed -- a rolled root opens under the closing boundary's own"
            " artifacts (period-model ss7, PR-22)"
        )
    # the COPY is checked, not the original: what this root will resume
    # from, audit against and hand to a second roll is the bytes that
    # landed here, and a copy nobody read is a copy nobody validated
    if read_seal(target, period_id).digest != seal.digest:
        raise EngineError(
            f"{seal_path(target, period_id)}: the imported sidecar digests to"
            f" something other than {seal.digest} -- the copy is not the seal this"
            " roll opened from (period-model ss7)"
        )
    verify_attestation(target, period_id)


def check_roll_target(new_root: Path, anchor_dir: Path) -> None:
    """What `run --open-from` refuses BEFORE it acts, in the caller's own
    words: a target that is the root this lineage just closed in.

    Rolling a root into itself is not a roll; it is `--resume` spelled
    dangerously, and the ownership rule would refuse it later with a
    sentence about claims that does not name the mistake."""
    stored = EstateAnchor(anchor_dir).read()
    if stored is None or not isinstance(stored.head, ClosedHead):
        return
    if normalized_root(stored.head.closing_root) == normalized_root(new_root):
        raise EngineError(
            f"{new_root} is the root this lineage closed period"
            f" {stored.head.period_id} in: opening the next period HERE is `dsl41 run"
            " --resume`, and a physical roll needs a different directory"
            " (period-model ss7)"
        )


# ---------------------------------------------------------- ss11 adoption


@dataclass(frozen=True)
class Adoption:
    """A legacy root, fenced and translated: everything ss11 steps 1-6
    produce, and what step 7 needs to seal it."""

    estate_id: str
    anchor: EstateAnchor
    #: period 1's committed manifest, built from the legacy `manifest/`
    manifest: Manifest
    #: C1, loaded from the legacy root's own stored inputs
    catalog: CatalogIR
    #: the boundary request step 7 seals under -- id derived, actor the
    #: invoking operator's, `force_seal` the CLI flag
    request: SealRequest
    #: True when this run found the tombstone and continued rather than
    #: fencing a live legacy root
    resumed: bool
    #: True when period 1's boundary was ALREADY committed and this run
    #: only had to perform the head CAS the crashed one did not (ss11's
    #: matrix). There is nothing left to seal.
    sealed: bool = False


def adopt_legacy_root(
    legacy_root: Path,
    *,
    anchor_dir: Path,
    profile: RuntimeProfile,
    staged_manifest: StagedManifest,
    claimed_actor: str,
    force_seal: bool = False,
    at: datetime | None = None,
    crash_point: CrashPoint = no_crash,
) -> Adoption:
    """ss11 steps 1-6: lock, identify, drain, readiness, fence, authority,
    split and translate.

    Step 7 -- the seal -- is deliberately NOT here: it is the COMMON seal
    body, and running it means resuming an ordinary period-1 engine over
    the translated WAL and asking it for a boundary. A private seal path
    for adoption would be a second implementation of the one thing this
    whole model exists to have exactly one of (PR-48).

    **The order is the argument.** Readiness runs FIRST, over an in-memory
    reconstruction of the legacy state, so a C2 that cannot open refuses
    with the sentinel, the legacy WAL and the anchor untouched -- draft 15
    let adoption fence a legacy root and commit period 1 without ever
    running C2's readiness, and an unsupported artifact version surfaced
    only when period 2 refused to open. The fence follows, as a hard link
    plus a rename, so there is no instant at which `journal.jsonl` is
    absent and an old `run` could genesis here. Authority follows the
    fence, because before it no new authority exists and a re-run simply
    starts over.

    The split precedes the translation, which is the one place this differs
    from ss11's numbering: the synthesized `segment` record IS the
    manifest's fields, so the manifest has to exist before the record that
    copies it. Nothing observes the root between the two -- the head is
    `adopting` and `run --resume` refuses by name -- so the reordering
    changes no state any reader can reach.

    Every step is idempotent: a re-run finds the tombstone, reads
    `estate_id` back rather than minting a second, and continues from
    wherever it stopped."""
    at = at or datetime.now(UTC).replace(tzinfo=None)
    anchor = EstateAnchor(anchor_dir)
    anchor.acquire()
    try:
        return _adopt_under_lock(
            legacy_root,
            anchor=anchor,
            profile=profile,
            staged_manifest=staged_manifest,
            claimed_actor=claimed_actor,
            force_seal=force_seal,
            at=at,
            crash_point=crash_point,
        )
    finally:
        # the transaction ends here, and the SEAL that finishes adoption is
        # the common seal body run by an ordinary engine over the root this
        # produced -- which takes the lineage lock for its own process
        # lifetime. Holding on would exclude it: `flock` is per open file
        # description, so a second acquire in this same process is refused
        # like anyone else's. Nothing can act in the gap -- `run --resume`
        # refuses an `adopting` head by name, genesis refuses an existing
        # anchor, a roll refuses a head that is not `closed`, a reclaim
        # refuses one that is not `claimed` -- and the legacy root's
        # `leader.lock` is held by the caller throughout.
        anchor.release()


def _adopt_under_lock(
    legacy_root: Path,
    *,
    anchor: EstateAnchor,
    profile: RuntimeProfile,
    staged_manifest: StagedManifest,
    claimed_actor: str,
    force_seal: bool,
    at: datetime,
    crash_point: CrashPoint,
) -> Adoption:
    """ss11 steps 1-6 proper, with the lineage lock already held -- split
    from `adopt_legacy_root` so the acquire/release pairing is one readable
    block rather than a `finally` wrapped around a hundred lines."""
    existing = read_sentinel(legacy_root)
    if existing is not None and existing.adopted_from is None:
        raise EngineError(
            f"{sentinel_path(legacy_root)}: this root is already a periodized estate"
            f" ({existing.estate_id}) -- adoption translates a LEGACY `header` journal,"
            " and this one has been through it or was born native (period-model ss11)"
        )
    if existing is None and wal_path(legacy_root, GENESIS_SEGMENT_NO).exists():
        # a legacy root has no `wal/` -- that layout arrived with the
        # sentinel. A root with both is one somebody assembled by hand or a
        # half-finished adoption whose tombstone is gone, and translating
        # into a segment this transaction did not write would adopt a
        # stranger's records under this estate's name
        raise EngineError(
            f"{wal_path(legacy_root, GENESIS_SEGMENT_NO)} exists on a root with no"
            " sentinel: a legacy layout has no `wal/`, so this root is neither legacy"
            " nor adopted and adoption will not translate into a segment it did not"
            " write (period-model ss1.1, ss11)"
        )
    source_wal = legacy_journal(legacy_root) if existing is not None else sentinel_path(legacy_root)
    if not source_wal.exists():
        raise EngineError(
            f"{source_wal}: no journal to adopt -- `dsl41 estate adopt` translates an"
            " existing legacy run root, and an empty directory is `dsl41 run`'s"
            " (period-model ss11)"
        )
    records = read_journal(source_wal)
    header = records[0]
    if header.get("rec") != "header":
        raise EngineError(
            f"{source_wal}: opens with a {header.get('rec')!r} record -- adoption"
            " translates a legacy `header` journal, and a `segment` root is already"
            " periodized (period-model ss11)"
        )
    if header.get("state_machine_version") != STATE_MACHINE_VERSION:
        # ss11 step 5: the barrier runs UNDER the legacy state-machine
        # version, which must equal this binary's. One executable
        # implements exactly one, and translating under a different one
        # would replay the legacy log through semantics it never ran
        raise EngineError(
            f"{source_wal}: state_machine_version {header.get('state_machine_version')!r}"
            f" and this binary implements {STATE_MACHINE_VERSION}: adoption replays the"
            " legacy log under the version that wrote it (period-model ss11, ss2.1)"
        )
    catalog, sources = legacy_catalog(legacy_root)
    # ss11 step 1's in-memory reconstruction: a read-only replay, writing
    # nothing. Phase 1 classifies against the CARRIED live closure, and a
    # fresh C1 oracle knows nothing of legacy QUE_WAIT rows, armed latches,
    # timers or pending intent -- draft 17 classified first and would have
    # fenced the legacy root before discovering the real state
    oracle = Oracle(catalog)
    seed_local_executor(oracle.store, LOCAL_EXECUTOR_ID, at=opening_at(header))
    replay = replay_inputs(oracle, records)
    check_drained(legacy_root, records, replay, rows=oracle.store.job)
    estate_id = existing.estate_id if existing is not None else str(uuid.uuid4())
    baseline_id = str(header["baseline_id"])
    epoch = next_epoch(records)
    staged = StagedNextPeriod(
        **{name: getattr(staged_manifest, name) for name in StagedNextPeriod.model_fields}
    )
    request = SealRequest.for_adoption(
        estate_id=estate_id,
        baseline_id=baseline_id,
        epoch=epoch,
        next_period=staged,
        claimed_actor=claimed_actor,
        force_seal=force_seal,
    )
    executing = executing_jobs(replay.outbox, oracle.store.job)
    validate_staged(
        StagedContext(
            staged=staged,
            staged_bytes=staged_manifest,
            boundary_request=request.boundary_request,
            request_fingerprint=request.fingerprint,
            c1=Baseline(catalog=catalog, profile=profile),
            c2=load_staged_catalog(legacy_root, staged_manifest),
            carried_state=carried_from_oracle(
                oracle,
                now=at,
                pending_spawn=[j for j, state in executing.items() if state == "pending"],
                bound=[j for j, state in executing.items() if state == "applied"],
            ),
            decision_index=replay.decisions,
            state_machine_version=STATE_MACHINE_VERSION,
            at=at,
        )
    )
    crash_point("after_readiness")
    stored_anchor = anchor.read()
    if stored_anchor is not None and stored_anchor.estate_id != estate_id:
        # BEFORE the fence: draft-order matters here exactly as it did for
        # authority -- fencing first and refusing at `create_adopting`
        # would leave the legacy root rewritten under an anchor that was
        # never this estate's (period-model ss1.3, ss11)
        raise EngineError(
            f"{anchor.path}: already holds estate {stored_anchor.estate_id} -- adoption"
            " creates its lineage in an EMPTY anchor, and this one is somebody's"
            " (period-model ss1.1, ss11)"
        )
    if stored_anchor is None and existing is not None:
        # an adopted sentinel with an EMPTY anchor is EITHER the crash
        # window between the fence and `create_adopting` -- the binding the
        # fence wrote names THIS anchor, and the retry proceeds -- or a
        # retry pointed at the WRONG anchor directory, where re-running
        # would mint a second closed authority over one root (the ss1.3
        # fork). The durable binding is what tells them apart.
        bound = existing.adopted_anchor
        if bound is None or bound != normalized_root(anchor.dir):
            raise EngineError(
                f"{legacy_root}: the sentinel says this root was adopted into estate"
                f" {estate_id} and {anchor.path} holds no lineage -- a retry must name"
                f" the ORIGINAL --estate-anchor ({bound or 'unrecorded'}), not"
                " a fresh one (period-model ss1.3)"
            )
    if existing is None:
        fence_legacy_root(legacy_root, estate_id, anchor_dir=anchor.dir)
    crash_point("after_fence")
    anchor.create_adopting(estate_id=estate_id, root=legacy_root)
    crash_point("after_adopting")
    if close_committed_adoption(legacy_root, anchor, estate_id):
        # ss11's matrix row, tested BEFORE steps 5-6 and not after them:
        # period 1's boundary is committed, so its manifest and its segment
        # are what the seal was taken over, and rewriting either under a
        # re-run's flags would leave a period that can never be re-derived
        # -- unauditable, unrollable, unprunable, and reported as a success
        installed = read_period_manifest(legacy_root, GENESIS_PERIOD_ID)
        assert installed is not None  # a sealed period has one, by construction
        return Adoption(
            estate_id=estate_id,
            anchor=anchor,
            manifest=installed,
            catalog=catalog,
            request=request,
            resumed=True,
            sealed=True,
        )
    manifest = adopted_manifest(legacy_root, header, catalog, sources, profile)
    crash_point("after_split")
    translate(legacy_root, manifest, header=header, records=records, estate_id=estate_id)
    crash_point("after_translate")
    return Adoption(
        estate_id=estate_id,
        anchor=anchor,
        manifest=manifest,
        catalog=catalog,
        request=request,
        resumed=existing is not None,
        sealed=False,
    )


def close_committed_adoption(legacy_root: Path, anchor: EstateAnchor, estate_id: str) -> bool:
    """ss11's matrix row: adoption's `seal` record present, head still
    `adopting`.

    The boundary COMMITTED -- the sidecar and the record are durable -- and
    the process died before the anchor CAS. A re-run of `estate adopt`
    performs it, and `run --resume` refuses until it has, because
    `adopting` names exactly one recovery owner. Returns whether it did, so
    the caller reports a finished adoption rather than sealing a period
    that is already closed.

    The WAL is fsynced BEFORE the head moves, on the rule every head
    transition in this tree follows: the crashed sealer may have written
    the line and died before its own fsync, so "recovery read it" proves
    readable, not durable, and a CAS over a line a power cut then removes
    leaves a lineage whose naming seal is gone."""
    path = wal_path(legacy_root, GENESIS_SEGMENT_NO)
    if not path.exists():
        return False  # nothing translated yet: this is a first run, not a recovery
    committed = [record for record in read_journal(path) if record.get("rec") == "seal"]
    if not committed:
        return False
    record = committed[-1]
    check_seal_record(record)
    # a shape-valid record is not a committed boundary: the sidecar it
    # names must exist, mirror it field for field, and BE this adoption's
    # -- closing the head over a record whose sidecar is missing or a
    # stranger's would commit a lineage whose boundary can never open
    sidecar = read_seal(legacy_root, GENESIS_PERIOD_ID)
    check_record_names_sidecar(sidecar, record, legacy_root)
    if sidecar.boundary_request.source != "adopt":
        raise EngineError(
            f"{seal_path(legacy_root, GENESIS_PERIOD_ID)}: source"
            f" {sidecar.boundary_request.source!r} on an adopting head -- this seal is"
            " not the adoption's (period-model ss11)"
        )
    if sidecar.estate_id != estate_id:
        raise EngineError(
            f"{seal_path(legacy_root, GENESIS_PERIOD_ID)}: estate {sidecar.estate_id}"
            f" under a sentinel naming {estate_id} -- a stranger's sidecar closes"
            " nothing here (period-model ss11)"
        )
    successor = read_period_manifest(legacy_root, sidecar.next_period.period_id)
    if successor is None:
        raise EngineError(
            f"{legacy_root}: period {sidecar.next_period.period_id} has no committed"
            " manifest -- the boundary this record names cannot open, and closing the"
            " head over it would commit an unopenable lineage (period-model ss11)"
        )
    # presence is not agreement: a self-consistent REPLACEMENT manifest
    # passes every shape check and still refuses at `open_from_seal` after
    # the head has closed -- so the full seal-to-opening validation runs
    # HERE, before the CAS commits a lineage that cannot open
    open_from_seal(sidecar, expected_digest=sidecar.digest, manifest=successor)
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    anchor.close_period(
        estate_id=estate_id,
        period_id=GENESIS_PERIOD_ID,
        root=legacy_root,
        seal_digest=sidecar.digest,
    )
    return True


def legacy_journal(legacy_root: Path) -> Path:
    """Where the fence put the original: `legacy/journal.jsonl`, hard-linked
    from the name the sentinel now occupies (ss11 step 3)."""
    return legacy_root / "legacy" / "journal.jsonl"


def legacy_catalog(legacy_root: Path) -> tuple[CatalogIR, list[SourceFile]]:
    """C1, from the legacy root's OWN stored inputs (DL-66's `manifest/`).

    Not from estate files on the command line: the run root outlives the
    files it was launched from, and the legacy build stored exactly what it
    loaded. `permit_unknown` is on, because adoption must not refuse an
    estate for a DL-07 attribute the old build already carried into its
    log.

    Parsed under the ORIGINAL paths DL-66's `manifest.json` recorded, and
    under the stored names where it recorded none: `catalog_hash` v2 covers
    spans and a span names its file, so the paths decide the number this
    period is pinned to for the rest of its life.

    The header's v1 hash is carried OPAQUE, as `catalog_hash_v1` on the
    segment, and is deliberately not re-verified: v1 hashes `meta`, so it
    moves with the installed package version, and an adoption is by
    definition an old root met by a new binary. Refusing on it would refuse
    every adoption there will ever be -- the outage v2 exists to end."""
    sources = legacy_sources(legacy_root)
    if not sources:
        raise EngineError(
            f"{legacy_root}: no stored inputs -- adoption replays the legacy log"
            " through the catalog that produced it, and this root no longer holds one"
            " (period-model ss11)"
        )
    try:
        parsed = [parse(source.text, file=source.path) for source in sources]
        return lower_catalog(parsed, permit_unknown=True), sources
    except (JilParseError, LoweringError) as exc:
        raise EngineError(
            f"{legacy_root}: the stored inputs do not load ({exc}): adoption cannot"
            " replay a log whose catalog it cannot rebuild (period-model ss11)"
        ) from exc


def legacy_profile(legacy_root: Path, attested: RuntimeProfile) -> RuntimeProfile:
    """The LEGACY period's `RuntimeProfile`: what DL-66's `manifest/`
    recorded, over what the operator attested.

    The manifest's `options` block holds four of the fields -- `timezone`,
    `as_machine`, `machine_policy` and `detached` -- and it is the estate's
    own record of how it was launched, so it wins. The rest (the deadman,
    the timezone table, the grace and settle windows) it never held, and
    those come from the flags: `estate adopt` is where an operator states
    them, and the resume gate compares the wiring against the pin, loudly,
    before anything is sealed.

    A root with no `options` block -- pruned, hand-made, or written by a
    build older than DL-66 -- yields the attestation unchanged."""
    payload = load_json(legacy_root / "manifest" / "manifest.json") or {}
    options = payload.get("options")
    if not isinstance(options, dict):
        return attested
    values: dict[str, Any] = attested.model_dump()
    if isinstance(options.get("timezone"), str):
        values["default_tz"] = options["timezone"]
    listed = options.get("as_machine")
    if isinstance(listed, list) and all(isinstance(name, str) for name in listed):
        values["as_machine"] = tuple(listed)
    if options.get("machine_policy") in ("strict", "local-eligible"):
        values["machine_policy"] = options["machine_policy"]
    if isinstance(options.get("detached"), bool):
        values["execution_mode"] = "detached" if options["detached"] else "tethered"
    return RuntimeProfile.model_validate(values)


def legacy_sources(legacy_root: Path) -> list[SourceFile]:
    """DL-66's `manifest/` as this build's `SourceFile` vector, in the
    command-line order the legacy manifest recorded.

    `original_path` is DL-66's own field and is preferred over the stored
    copy's name for exactly the reason DL-130 kept recording it: the hash
    covers spans, and a span names the file it was parsed from."""
    payload = load_json(legacy_root / "manifest" / "manifest.json") or {}
    listed = payload.get("sources")
    if isinstance(listed, list) and listed:
        # DL-66's own record, read DIRECTLY: the fence-to-authority crash
        # window has a `period_root` sentinel and no `wal/` yet, so the
        # layout walk (`stored_input_paths` -> active period) cannot run
        # there -- and `manifest/` is exactly what adoption is defined over
        stored = [legacy_root / "manifest" / str(entry.get("file")) for entry in listed]
        if all(path.is_file() for path in stored):
            return [
                SourceFile(
                    path=str(entry.get("original_path") or entry.get("file")),
                    text=path.read_text(encoding="utf-8"),
                )
                for entry, path in zip(listed, stored, strict=True)
            ]
    # a retry after the split: `manifest/` is gone and the bundle layout
    # holds the inputs -- the ordinary walk reads it
    stored = stored_input_paths(legacy_root)
    return [SourceFile(path=str(path), text=path.read_text(encoding="utf-8")) for path in stored]


def check_drained(
    legacy_root: Path,
    records: list[dict[str, Any]],
    replay: Replay,
    *,
    rows: Mapping[str, JobRuntime],
) -> None:
    """ss11 step 2: require a drained and settled estate.

    Four refusals, and every one of them is a refusal rather than a repair.
    An admitted input with no `result` cannot be "given a decision": replay
    recovers only the `ApplyResult` and discards the emitted events that
    would plan its effects, so an adopter that decided it either dispatched
    a recovered SPAWN before its decision was durable or wrote
    `effects: []` and let reconciliation fail a start the old estate had
    committed. A live detached wrapper can outlive the engine that released
    the lock, so "no engine" is not "no work". And a live legacy FW's
    progress exists only in an adapter's local variable, so it is not
    reconstructible at all.

    Adoption is a one-time migration; a drain is what the runbook already
    does at every release."""
    # the DURABLE decisions, read straight off the records: `replay` has
    # already recovered a result-less attempt's decision by re-running the
    # gate, which is right for replay and exactly wrong here -- the whole
    # question is whether the legacy engine wrote one
    durable = {
        record["index"]
        for record in records
        if record.get("rec") in ("decision", "result") and isinstance(record.get("index"), int)
    }
    undecided = sorted(
        attempt.index for attempt in read_attempts(records) if attempt.index not in durable
    )
    if undecided:
        raise EngineError(
            f"{legacy_root}: admitted input(s) {undecided} have no durable `result`:"
            " adoption refuses rather than deciding them -- replay recovers the"
            " decision and discards the effects it would have planned, so either a"
            " recovered SPAWN is dispatched before its decision is durable or a start"
            " the old estate committed is failed. Resume the legacy engine, let it"
            " settle, and retry (period-model ss11)"
        )
    pending = [effect.effect_id for effect in replay.outbox.pending()]
    if pending:
        raise EngineError(
            f"{legacy_root}: the legacy outbox still holds {', '.join(pending)}:"
            " adoption refuses a pending legacy outbox -- resume the legacy engine,"
            " let it settle, and retry (period-model ss11)"
        )
    _check_supervisor_drained(legacy_root)
    for effect, state in live_spawns(replay.outbox, rows):
        if state != "applied":
            continue
        run_dir = legacy_root / "runs" / f"{effect.job}.{effect.run_number}"
        if (run_dir / WATCH_LOG).exists():
            raise EngineError(
                f"{legacy_root}: {effect.job}.{effect.run_number} is a LIVE file watch:"
                " an FW poll's progress exists only in an adapter's local variable and"
                " is not reconstructible, so adoption refuses rather than restarting"
                " the watch under a new period (period-model ss11)"
            )
        spawn = load_json(run_dir / "spawn.json")
        if spawn is None:
            continue  # nothing alive to prove; the barrier resolves it
        pid, token = spawn.get("wrapper_pid"), spawn.get("start_token")
        if isinstance(pid, int) and isinstance(token, str) and verify_alive(pid, token):
            raise EngineError(
                f"{legacy_root}: {effect.job}.{effect.run_number} still has a live"
                f" wrapper (pid {pid}): a detached wrapper outlives the engine that"
                " released the lock, so a free `leader.lock` is not a drained estate."
                " Let it finish or kill it, then retry (period-model ss11)"
            )


def _check_supervisor_drained(legacy_root: Path) -> None:
    """The LIST half of ss11 step 2: a legacy supervisor can hold a live
    wrapper the local spool never recorded (a crash before `spawn.json`,
    or a stale one), so "the spool shows nothing alive" is not "nothing is
    alive". Read-only: one LIST over the existing socket, never a start.

    A socket that exists and does not answer refuses: the supervisor that
    owns the evidence is unreachable, so a drained estate cannot be
    proved (the ss8 rule, applied to adoption's one-time drain)."""
    sock = legacy_root / "supervisor.sock"
    if not sock.exists():
        return  # tethered estate, or the supervisor is gone WITH its wrappers
    try:
        conn = SupervisorConn(sock)
        try:
            listing = conn.send({"cmd": "LIST"})
        finally:
            conn.close()
    except OSError as exc:
        raise EngineError(
            f"{sock}: a supervisor socket exists and does not answer ({exc}): the"
            " process that owns the live-wrapper evidence is unreachable, so a"
            " drained estate cannot be proved. Reach it or shut it down, then retry"
            " (period-model ss11, ss8)"
        ) from exc
    runs = listing.get("runs")
    well_formed = (
        listing.get("ok") is True
        and listing.get("version") == SUPERVISOR_PROTOCOL_VERSION
        and isinstance(runs, list)
        and all(
            isinstance(row, dict)
            and isinstance(row.get("job"), str)
            and isinstance(row.get("run_number"), int)
            and isinstance(row.get("wrapper_alive"), bool)
            for row in runs
        )
    )
    if not well_formed or not isinstance(runs, list):  # the isinstance is for mypy
        # an error envelope, an unknown protocol version, or a row shape
        # missing the one flag this check reads is NOT an empty estate: a
        # missing `wrapper_alive` read as false would fence over an
        # unspooled live wrapper
        raise EngineError(
            f"{sock}: the supervisor did not answer a well-formed LIST"
            f" ({listing!r}): a drained estate cannot be proved from an answer this"
            " binary does not fully understand (period-model ss11, ss8)"
        )
    alive = sorted(
        f"{row.get('job')}.{row.get('run_number')}" for row in runs if row.get("wrapper_alive")
    )
    if alive:
        raise EngineError(
            f"{legacy_root}: the supervisor still lists live wrapper(s)"
            f" {', '.join(alive)}: adoption requires a drained estate -- let them"
            " finish or kill them, then retry (period-model ss11)"
        )


def fence_legacy_root(legacy_root: Path, estate_id: str, *, anchor_dir: Path) -> None:
    """ss11 step 3: hard-link the legacy journal aside, then rename the
    sentinel over its name.

    **There is no instant at which `journal.jsonl` is absent**, which is
    the whole point of the order: an old `run` on a journal-less root
    starts a NEW genesis and writes `manifest/` before it takes the lock.
    After this step an old `run` refuses (a `journal.jsonl` exists) and an
    old `run --resume` refuses (its first record is not `header`).

    Both writes are the liturgy. A bare `link` plus `os.replace` passes
    every process-kill test, and a power loss can restore the header
    pathname after period 2 has opened."""
    target = legacy_journal(legacy_root)
    mkdir_durable(str(target.parent))
    os.chmod(target.parent, 0o700)
    source = sentinel_path(legacy_root)
    if target.exists():
        # a retry meets its own earlier link -- and ONLY that. The archived
        # name must be the SAME inode as the journal it archives: a foreign
        # file under this name would be trusted as the legacy WAL forever,
        # and the rename below would then delete the only real copy
        src_stat, dst_stat = os.stat(source), os.stat(target)
        if (src_stat.st_dev, src_stat.st_ino) != (dst_stat.st_dev, dst_stat.st_ino):
            raise EngineError(
                f"{target}: exists and is not the same file as {source} -- the"
                " archived name must BE the legacy journal, and adoption refuses to"
                " replace the only copy over a stranger's file (period-model ss11)"
            )
    else:
        os.link(source, target)
    # unconditionally, not only on the create: a retry after a failed fsync
    # would otherwise skip the one act that makes the archived link durable,
    # and the sentinel rename below could then survive a power cut the link
    # does not
    fsync_dir(target.parent)
    # the ORIGINAL anchor, bound IN the sentinel and by the same atomic
    # rename: a crash between this fence and `create_adopting` leaves an
    # adopted sentinel with the correct anchor still empty, and without
    # the binding a retry cannot tell that crash window from a retry
    # pointed at the WRONG anchor -- one must proceed, the other is the
    # ss1.3 fork. A side file would be swappable under an untouched
    # sentinel; a field of the sentinel is replaced only by replacing the
    # root's one ownership record itself.
    write_sentinel(
        legacy_root,
        Sentinel(
            estate_id=estate_id,
            adopted_from="legacy/journal.jsonl",
            adopted_anchor=normalized_root(anchor_dir),
        ),
    )


def adopted_manifest(
    legacy_root: Path,
    header: Mapping[str, Any],
    catalog: CatalogIR,
    sources: Sequence[SourceFile],
    profile: RuntimeProfile,
) -> Manifest:
    """ss11 step 6: split `manifest/` into `catalogs/<bundle>/` and
    `periods/000001/` -- a CONSTRUCTION, not a rename.

    The legacy directory is left where it is. It is the record of what the
    old estate loaded, the hard-linked journal beside it is the record of
    what the old estate did, and neither is this build's to delete.

    `baseline_id` is the header's, preserved: every fingerprint in the
    legacy log was composed under it, and minting a new one would make the
    translated records unverifiable against their own attempts."""
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(legacy_root, sources),
        profile=profile,
        state_machine_version=STATE_MACHINE_VERSION,
    )
    manifest = staged.commit(
        period_id=GENESIS_PERIOD_ID,
        baseline_id=str(header["baseline_id"]),
        clock_domain=str(header["clock_domain"]),
        segment_no=GENESIS_SEGMENT_NO,
        first_index=GENESIS_FIRST_INDEX,
    )
    installed = read_period_manifest(legacy_root, GENESIS_PERIOD_ID)
    if installed is not None and installed != manifest:
        # a re-run under DIFFERENT flags. The translated segment already
        # pins the first attempt's hashes, so overwriting would leave the
        # manifest and the record describing two different periods --
        # loud here, where the operator can still change the flag back
        moved = sorted(
            name
            for name in type(manifest).model_fields
            if getattr(manifest, name) != getattr(installed, name)
        )
        raise EngineError(
            f"{legacy_root}: periods/000001/manifest.json already pins this adoption"
            f" and this run disagrees on {', '.join(moved)} -- a re-run continues the"
            " transaction it found, it does not re-describe the period"
            " (period-model ss11)"
        )
    write_period_manifest(legacy_root, manifest)
    return manifest


def translate(
    legacy_root: Path,
    manifest: Manifest,
    *,
    header: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    estate_id: str,
) -> Path:
    """ss11 step 5's second half: write `wal/000001.jsonl` by the liturgy.

    A synthesized `segment` from the `header`, every
    `input`/`advance`/`host`/`leader`/`dispatch`/`drop`/`preflight`/
    `effect_result` copied VERBATIM, and every `result` plus its same-index
    `effect` records folded into one `decision` line marked
    `legacy_batch: true` -- because those records were separate fsyncs and
    a fold cannot make a torn batch atomic after the fact. Audit knows the
    difference.

    Idempotent by existence: a re-run over a root whose segment is already
    there leaves it alone. It must, because by then the adopter's own
    `leader` record and the barrier's injections may be in it, and
    re-translating would drop them."""
    path = wal_path(legacy_root, GENESIS_SEGMENT_NO)
    if path.exists():
        return path
    lines = [
        segment_record(
            manifest,
            estate_id=estate_id,
            at=opening_at(header),
            catalog_hash_v1=str(header["catalog_hash"]),
        )
    ]
    lines.extend(fold_legacy(legacy_root, records))
    open_wal(legacy_root, GENESIS_SEGMENT_NO)
    # `durable_write` creates its temp 0600 and renames, so the segment is
    # owner-only from the instant it exists -- the WAL carries globals and
    # every control input, and a chmod after the rename would be a window
    durable_write(str(path), b"".join(canonical_bytes(record) + b"\n" for record in lines))
    return path


def fold_legacy(legacy_root: Path, records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The record-by-record translation, as a pure function of the legacy
    log plus the spool.

    `run_id` on a folded SPAWN is read from `spawn.json` -- the legacy
    estate had exactly one executor at generation 0, and the spool is where
    its adapter recorded the run, so both are defined reconstructions
    rather than guesses. `null` where no `spawn.json` exists AND the
    effect's recorded outcome is `retired` or `indeterminate`: a legacy
    SPAWN that a drain and a KILL retired before it reached an adapter
    legitimately has no run and no file, and adoption must not refuse an
    estate for a run that never existed."""
    effects: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("rec") == "effect":
            effects.setdefault(int(record["index"]), []).append(
                {k: v for k, v in record.items() if k != "rec"}
            )
    outcomes = {
        str(record["effect_id"]): str(record.get("state"))
        for record in records
        if record.get("rec") == "effect_result"
    }
    out: list[dict[str, Any]] = []
    for record in records:
        kind = record.get("rec")
        if kind in ("header", "effect"):
            continue  # the segment replaces one; the other folds below
        if kind != "result":
            out.append(dict(record))
            continue
        index = int(record["index"])
        out.append(
            {
                "rec": "decision",
                "index": index,
                "request_id": record["request_id"],
                "decision": record["decision"],
                "reason": record.get("reason"),
                "revisions": record.get("revisions") or {},
                "legacy_batch": True,
                "effects": [
                    _folded_effect(legacy_root, effect, outcomes)
                    for effect in effects.get(index, [])
                ],
            }
        )
    return out


def _folded_effect(
    legacy_root: Path, effect: Mapping[str, Any], outcomes: Mapping[str, str]
) -> dict[str, Any]:
    folded = dict(effect)
    folded["generation"] = 0
    if folded.get("kind") != "SPAWN" or folded.get("run_id") is not None:
        return folded
    spawn = load_json(
        legacy_root / "runs" / f"{effect['job']}.{effect['run_number']}" / "spawn.json"
    )
    if spawn is not None and not (
        spawn.get("job") == effect["job"] and spawn.get("run_number") == effect["run_number"]
    ):
        # DL-118 at the fold: a spawn.json naming another (job, run_number)
        # is a stranger's record, and copying its run_id would forge a
        # durable binding every later identity check then trusts
        spawn = None
    run_id = spawn.get("run_id") if spawn is not None else None
    if run_id is None and outcomes.get(str(effect["effect_id"])) not in (
        "retired",
        "indeterminate",
    ):
        raise EngineError(
            f"{legacy_root}: SPAWN {effect['effect_id']} has no spawn.json and its"
            f" outcome is {outcomes.get(str(effect['effect_id']))!r} -- a null run_id is"
            " legal only for a run that provably never reached an adapter"
            " (period-model ss11)"
        )
    folded["run_id"] = run_id
    return folded
