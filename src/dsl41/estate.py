"""The physical roll: opening a lineage's next period in a fresh root.

Normative spec: `docs/period-model.md` ss1.1 (the sentinel and the one ownership rule), ss1.3
(the successor fence, the physical roll's attestation gate) and ss7 (the two openers). Built by
DL-134 as U7. Obligations PR-01c, PR-02a, PR-02d in ss13.

The roll takes possession of a root that is not yet this estate's period, fences it, and hands
the ordinary machinery a root it can resume. It is not a second semantic path: the seal and
opening format are identical whether the next period continues in place or opens a fresh root
(PR-07). There is no adoption from a legacy estate (DL-138).

**The order is the whole argument** (ss7): new-root `leader.lock`, sentinel durable,
`anchor.lock` and the claim, the import, the segment, the head. The sentinel goes before the
claim. Claim-first would let B move the head to `claimed(B)`, die before its sentinel, and leave
a root an old binary treats as unused and geneses into, which after a `reclaim` is a fork. The
head is never `claimed(target_root)` while `target_root` lacks a valid sentinel (PR-01a).
"""

from __future__ import annotations

import os

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dsl41.attest import verify_attestation
from dsl41.boundary import (
    Anchor,
    ClaimedHead,
    ClosedHead,
    CommittedBoundary,
    CrashPoint,
    EstateAnchor,
    check_opening_version,
    claim_id_for,
    claim_root,
    OpenHead,
    no_crash,
    normalized_root,
    open_next_period,
    read_seal,
    seal_path,
    spell_head,
)
from dsl41.ir import CatalogIR
from dsl41.period import (
    Manifest,
    attestation_path,
    bundle_sources,
    period_dir,
    read_period_manifest,
    seal_dir,
    write_bundle,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_ledger import Proof
from dsl41.runner_procid import durable_write, mkdir_durable
from dsl41.seal import OpenedRuntime, Seal


@dataclass(frozen=True)
class Rolled:
    """A physical roll, done: the new root holds period N+1.

    It carries no `Journal`: the roll writes the opening `segment` and hands the root to the
    ordinary resume path, which opens its own.
    """

    #: the seal that was imported and opened from
    seal: Seal
    #: where it came from, so the caller can say so
    closing_root: Path
    #: ss7 phase 3's load over that seal: the identity the new period opened under
    opened: OpenedRuntime


def roll_into_root(
    new_root: Path,
    *,
    anchor_dir: Path,
    catalog_of: Callable[[Path, Manifest], CatalogIR],
    lock: Proof | None = None,
    crash_point: CrashPoint = no_crash,
    admit: Callable[[Manifest], None] | None = None,
) -> Rolled:
    """ss7's second opener: open `next_period` into a fresh root.

    Reads the lineage head, requires it `closed`, requires the closing period fully quiescent
    (ss8: no live executions) and attested (ss1.3), imports the four artifacts the new root
    needs, then runs the opening the in-place path runs.

    Attested means `run --open-from` finds `seals/<N>.audit.json` in `closing_root` and it
    passes `verify`; a file that merely exists is not enough (PR-02d).

    The caller already holds `leader.lock` on `new_root`, because starting a supervisor or
    staging bytes into a root is an act on an estate this process may not lead.

    `admit` is the caller's gate on the manifest this roll opens under. It runs on the selected
    seal before the first write and refuses by raising `EngineError`. The claim refuses a head
    that does not name this seal, so a passing gate judged the period that opens, and a
    refusal leaves no segment and an unmoved head (PR-22b).
    """
    # read before the lock: the claim is a compare-and-swap that re-reads under it and refuses a
    # head that moved, so this read only has to yield a claim_id; the sentinel is durable before
    # the claim exists (PR-01a)
    anchor, closing_root, seal = _roll_source_or_refuse(anchor_dir, new_root)
    opening = seal.next_period
    manifest = _successor_manifest(closing_root, seal)
    if admit is not None:
        # The gate may PING a supervisor at the target. One started after the PING and before the
        # writes is not seen here; the run's post-wiring gate refuses it, with the period already
        # open. Closing the window needs a lock a starting supervisor does not take, so the
        # few-write window is accepted.
        admit(manifest)
    claim_id = claim_id_for(
        prev_seal_digest=seal.digest,
        next_period=opening.period_id,
        target_root=new_root,
    )
    # the target may not exist yet and the sentinel is the first thing written into it, so every
    # created component is made durable here, unconditionally, so a retry after a failed fsync
    # repairs it. `acquire_run_root` does the same for the caller that took the lock
    mkdir_durable(str(new_root))
    os.chmod(new_root, 0o700)
    claim_root(new_root, estate_id=seal.estate_id, claim_id=claim_id)
    crash_point("after_roll_sentinel")
    anchor.acquire()
    committed = CommittedBoundary(seal=seal, manifest=manifest)

    def imported() -> CatalogIR:
        # ss7 puts the import between the claim and the segment; it is idempotent by content
        # address, so a re-import after a crash writes the same bytes to the same names
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
        # the roll is a transaction and ends here: the head is `open(N+1, new_root)` and its
        # segment is durable. The engine that resumes this root takes the lineage lock for its
        # lifetime; `flock` is per open file description, so holding on would exclude it. Nothing
        # acts in the gap: an `open` head refuses genesis, a claim, a second roll and a reclaim,
        # and `leader.lock` on `new_root` stays held
        anchor.release()
    period.journal.detach()  # the descriptor, not the term: the resume opens its own
    return Rolled(seal=seal, closing_root=closing_root, opened=period.opened)


def _roll_source_or_refuse(anchor_dir: Path, new_root: Path) -> tuple[EstateAnchor, Path, Seal]:
    """The read-only half of a physical roll: the lineage anchor, the root holding the closing
    seal, and the seal, checked, or a refusal naming what is wrong.

    `check_roll_ready` runs this pass and the successor's manifest read, so a refusal writes
    nothing; `roll_into_root` runs it again under the locks. One copy keeps one wording of each
    refusal (DL-152).
    """
    anchor = EstateAnchor(anchor_dir)
    stored = anchor.read()
    if stored is None:
        raise EngineError(
            f"{anchor.path}: this lineage has no anchor -- a physical roll opens the"
            " successor of a seal, and there is no lineage here to succeed"
            " (period-model ss1.3)"
        )
    closing_root, period_id, seal_digest = _roll_source(stored, anchor, new_root)
    seal = read_seal(closing_root, period_id)
    # the version first: this half runs before the roll writes anything (DL-253)
    check_opening_version(seal.next_period, where=str(closing_root))
    if seal.digest != seal_digest:
        raise EngineError(
            f"{seal_path(closing_root, period_id)}: digest {seal.digest} but the"
            f" head says {seal_digest} -- the closing root does not hold the seal"
            " this lineage closed with (period-model ss11)"
        )
    if seal.executions:
        # ss8's mode table: a physical roll while jobs are live is refused. The supervisor is one
        # per run root and a new-root engine cannot reach the old root's work; the bridge that
        # would lift this is a non-goal (ss12)
        live = ", ".join(f"{entry.job}.{entry.run_number}" for entry in seal.executions)
        raise EngineError(
            f"seal {seal.digest} carries live execution(s) ({live}): a physical roll"
            " needs every execution terminal, because the supervisor is one per run"
            " root and a new-root engine cannot reach the old root's work"
            " (period-model ss8)"
        )
    verify_attestation(closing_root, period_id)
    return anchor, closing_root, seal


def _successor_manifest(closing_root: Path, seal: Seal) -> Manifest:
    """The committed manifest of the period `seal` opens, read from the root that closed it.

    The roll opens under it, and the read-only preflight hands it to the caller so the launch
    options are held to the pin before anything is written (PR-22b).
    """
    opening = seal.next_period
    manifest = read_period_manifest(closing_root, opening.period_id)
    if manifest is None:
        raise EngineError(
            f"{closing_root}: periods/{opening.period_id:06d}/manifest.json is not"
            " there -- the boundary installed it before the record that names it, so"
            " a roll that cannot find it is rolling from a pruned root"
            " (period-model ss7)"
        )
    return manifest


def _roll_source(stored: Anchor, anchor: EstateAnchor, new_root: Path) -> tuple[Path, int, str]:
    """Which seal this roll opens, and where it lives: `(closing_root, closing period, digest)`.

    Two heads answer it. `closed` is the ordinary start. `claimed` by this root is our own claim
    resumed: ss1.3 makes the claim idempotent on `claim_id`, so a crash between the claim and
    the segment re-runs here, and the claim file carries the seal it was taken against.
    Everything else refuses; the completed case names the verb that continues it.
    """
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
        f"{anchor.path}: the head is {head.state} and this root does not hold it"
        f" ({spell_head(head)}): a physical roll opens the period a committed seal"
        " left unopened, or resumes its own claim, and nothing else (period-model ss7)"
    )


def import_boundary(source: Path, target: Path, *, seal: Seal, manifest: Manifest) -> None:
    """ss1.3's import: the four artifacts a rolled root needs to be resumable on its own.

    These are `seals/<N>.json`, `seals/<N>.audit.json`, `catalogs/<C2 bundle>/` and
    `periods/<N+1>/`. The import is idempotent by content address: the bundle is
    re-materialized from its recorded bytes and the three files are rewritten over whatever a
    crashed import left, so a re-import writes the same bytes (ss11's matrix row).

    C1's WAL and C1's bundle are not imported: importing C1's proof set on every roll is
    retention policy, not a boundary mechanism. The attestation carries the proof, and the
    anchor registry is how a caller who retained C1 finds it (ss1.3).
    """
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
        # the new period opens under the import, so one that differs from the boundary's own
        # manifest is refused before a segment names it
        raise EngineError(
            f"{destination}: the imported manifest is not the one the boundary"
            " committed -- a rolled root opens under the closing boundary's own"
            " artifacts (period-model ss7, PR-22)"
        )
    # the copy is checked, not the original: this root resumes from, audits against and hands
    # to a second roll the bytes that landed here
    if read_seal(target, period_id).digest != seal.digest:
        raise EngineError(
            f"{seal_path(target, period_id)}: the imported sidecar digests to"
            f" something other than {seal.digest} -- the copy is not the seal this"
            " roll opened from (period-model ss7)"
        )
    verify_attestation(target, period_id)


def check_roll_ready(new_root: Path, anchor_dir: Path) -> Manifest:
    """`run --open-from`'s read-only preflight, run by the CLI before it creates the new root or
    takes its lock.

    The lineage exists, the head is a closed one this roll can succeed, the closing seal is the
    head's and carries nothing live, and the closing period is attested. Returns the committed
    manifest of the period the roll would open; the caller holds its launch options to that pin,
    because a refusal after the roll would leave the successor open (period-model ss7, PR-22b).

    Every check re-runs under the locks in `roll_into_root`. This pass makes a refusal, such as
    the unattested-roll refusal, write nothing, not even the target directory. Running early is
    sound because attestation is never revoked and a head that moves meanwhile is refused under
    the lock.
    """
    check_roll_target(new_root, anchor_dir)
    _, closing_root, seal = _roll_source_or_refuse(anchor_dir, new_root)
    return _successor_manifest(closing_root, seal)


def check_roll_target(new_root: Path, anchor_dir: Path) -> None:
    """What `run --open-from` refuses before it acts: a target that is the root this lineage just
    closed in.

    Rolling a root into itself is `--resume` spelled dangerously; the ownership rule would
    refuse it later in words about claims that do not name the mistake.
    """
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
