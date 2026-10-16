"""Retention floors, and the verb that prunes what is left (period-model ss11a, ss12; DL-135).

Retention policy (which periods, spools and tombstones to keep, and for how long) is a non-goal
(ss12); it lives in `deployment-runbook.md` ss2a as operator flags. This module computes what may
never be pruned.

The floor is everything reachable from the lineage head: the sentinel; the anchor and any active
claim; the seal sidecar the current period opened from and the one it will close with; the
current and committed-next period manifests; an installed-but-uncommitted candidate's
`staged_manifest.json` and `candidate.json`; their catalog bundles and `sources.json`; the latest
attestation checkpoint and every attestation after it; the WAL and spool of any unattested
period; the spool of any live or carried execution; and any SPAWN tombstone whose effect can
still be replayed. Recovery refuses without a sidecar or a catalog directory, so those are
floored too (PR-36c).

Three verdicts:

- **floored** -- ss12 or ss11a forbids it. `prune` cannot reach it.
- **held** -- the floor has lifted (the head moved past it and a later checkpoint covers it) and
  no retention class licenses the deletion. Held is neither a refusal nor a permission. It
  covers what this version does not offer to delete (DL-144): a sidecar or manifest a later
  period's re-derivation reads, an earlier checkpoint, a content-addressed bundle, and
  anything whose archive an itemized dependency blocks.
- **prunable** -- a rule licenses deletion by name: a SPAWN tombstone whose period is attested
  and whose run is terminal (PR-36b, ss11a), a quarantined candidate no recovery references
  (ss12), and the inputs of an archived period under the `archive-inputs` class (DL-144).

The itemized obligation wins for the spool: ss12 floors the WAL and spool of any unattested
period, and PR-36b releases an attested period's run directory and index entry once the run is
terminal.

**The archive (ss12, DL-144).** PR-Q3 asks whether a seal-only archive may stand in for pruned
inputs. It may, conditionally and by explicit policy:

- **The receipt is the point of no return.** `seals/<period>.archive.json` is written durably
  before the first deletion and names the estate, the period, the seal digest, the attestation it
  stands on, the class, and the exact licensed artifact list. A retry after a partial sweep
  re-reads it and completes. Missing evidence with no receipt is loss and refuses.
- **The receipt, the period's attestation and its sidecar are a permanent floor.** `_remove`
  cannot reach them.
- **Eligibility is itemized.** Two artifact kinds are in the class: the WAL of a covered period,
  and a committed candidate's `staged_manifest.json` and `candidate.json` under the same cover.
  The WAL carries four dependencies of its own. Content-addressed bundles are out in v1: a bundle
  is shared by reference, and deciding its reachability across an archived period is a race this
  class does not take.
- **The act is per period and all-or-nothing.** A receipt exists exactly when a period's inputs
  were archived, so a reader reports a tier from it (`attest.verified_tier`).

`prune` iterates the plan's `prunable` items alone, and `_remove` refuses a path that is not one,
that is not under the run root, or that contains a floored or held artifact.
"""

from __future__ import annotations

import os
import stat as stat_module
import uuid
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Literal

from dsl41.attest import verify_archive_receipt, verify_attestation
from dsl41.boundary import (
    ANCHOR_LOCK_NAME,
    ANCHOR_NAME,
    Anchor,
    ClaimedHead,
    EstateAnchor,
    Head,
    PeriodRow,
    default_anchor_dir,
    normalized_root,
    registry_rows,
    read_candidate,
    read_seal,
    read_staged_manifest,
    check_record_names_sidecar,
    check_seal_record,
)
from dsl41.canon import is_wire_int
from dsl41.runner_effects import RUN_ID_RE
from dsl41.period import (
    ARCHIVE_CLASS,
    CANDIDATE_NAME,
    SENTINEL_NAME,
    STAGED_MANIFEST_NAME,
    WAL_DIR,
    ArchiveReceipt,
    archive_receipt_path,
    attestation_path,
    bundle_dir,
    period_dir,
    read_period_manifest,
    read_sentinel,
    seal_dir,
    seal_path,
    sentinel_path,
    wal_path,
    wal_segments,
    split_run_dir,
    write_archive_receipt,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_journal import dsl41_version, opens_with_rec, read_journal
from dsl41.seal import Seal

#: the run_id -> (job, run_number) index the supervisor writes first (ss11a). The supervisor
#: tier imports no dsl41 (DL-42), so this duplicates `runner_supervisor._INDEX_DIR`.
INDEX_DIR: Final[str] = ".by_run_id"

Verdict = Literal["floored", "held", "prunable"]

#: what an operator's flags select, and the kinds each covers. The same classes serve `--dry-run`
#: and a real run; which to remove is the operator's call.
CLASSES: Final[dict[str, tuple[str, ...]]] = {
    "tombstones": ("run", "run_index", "run_log"),
    "quarantine": ("quarantine",),
    #: the archive (DL-144): two kinds, with every eligibility condition itemized in
    #: `_archive_eligibility`
    ARCHIVE_CLASS: ("wal", "candidate"),
}


class ArtifactChanged(EngineError):
    """The disk does not hold the artifact the plan verified. The sweep records it and continues,
    as it does an OS refusal."""


@dataclass(frozen=True)
class Artifact:
    """One thing on disk and the retention verdict on it; `rule` cites what decided the verdict."""

    path: Path
    kind: str
    verdict: Verdict
    rule: str
    why: str
    period_id: int | None = None
    run: tuple[str, int] | None = None
    #: (st_dev, st_ino) at plan time, on prunable artifacts only. Removal verifies it through the
    #: held descriptor and refuses a floored artifact renamed onto a prunable name.
    ident: tuple[int, int] | None = None
    #: the snapshot identities at and beneath this artifact: the only inodes its removal may
    #: delete. Per artifact, so an inode moved inside this tree after planning refuses.
    licensed: frozenset[tuple[int, int]] | None = None

    def render(self) -> str:
        where = f"period {self.period_id}" if self.period_id is not None else "estate"
        return f"{self.verdict:9} {self.kind:11} {self.path}  [{where}, {self.rule}] {self.why}"


@dataclass(frozen=True)
class RetentionPlan:
    """What `prune` may do to one run root. Pure: nothing here writes, unlinks or takes a lock."""

    run_root: Path
    anchor_dir: Path
    estate_id: str
    #: (st_dev, st_ino) of the resolved root at plan time; a retargeted run-root symlink would
    #: otherwise pass every path-string containment check
    root_ident: tuple[int, int]
    #: identities of every floored and held artifact at plan time; recursive removal refuses any
    #: found beneath a deletion target
    retained_idents: frozenset[tuple[int, int]]
    #: the newest period this root holds a segment for (I1: segment N is period N)
    current_period: int
    #: every period whose attestation this root holds AND which verifies
    attested: frozenset[int]
    #: every period this root holds a bound archive receipt for (ss12, DL-144): its inputs are
    #: gone by policy and its attestation stands in for them
    archived: frozenset[int]
    artifacts: tuple[Artifact, ...]

    def floors(self) -> tuple[Artifact, ...]:
        return tuple(item for item in self.artifacts if item.verdict == "floored")

    def held(self) -> tuple[Artifact, ...]:
        return tuple(item for item in self.artifacts if item.verdict == "held")

    def prunable(self) -> tuple[Artifact, ...]:
        return tuple(item for item in self.artifacts if item.verdict == "prunable")

    def retained_over(self, path: Path) -> Artifact | None:
        """The retained artifact `path` would take with it: itself or one beneath it.

        Comparing paths for equality alone would let `runs/` go with floored tombstones under it."""
        target = _resolved(path)
        for item in self.artifacts:
            if item.verdict == "prunable":
                continue
            here = _resolved(item.path)
            if here == target or here.is_relative_to(target):
                return item
        return None


@dataclass(frozen=True)
class PruneReport:
    """What one `prune` call did. `kept` is prunable but outside the flags given; the other
    verdict fields say why each survivor survived."""

    removed: tuple[Artifact, ...]
    kept: tuple[Artifact, ...]
    floored: tuple[Artifact, ...]
    held: tuple[Artifact, ...]
    #: selected, licensed, and refused by the operating system, with the reason
    failed: tuple[tuple[Artifact, str], ...]
    #: selected and not licensed: the archive class was asked for and an itemized dependency is in
    #: the way (ss12, DL-144). Nothing was attempted; the reason names the order to follow.
    refused: tuple[tuple[Artifact, str], ...]
    bytes_removed: int
    dry_run: bool


# ------------------------------------------------------------- the plan


def plan_retention(run_root: Path, *, anchor_dir: Path | None = None) -> RetentionPlan:
    """Read one estate root and decide every artifact's verdict (ss12).

    The anchor is read and never locked: a live engine holds the lineage lock, and attestation is
    monotone, so a period seen as attested stays attested and anything newly created belongs to
    the open period, which is floored."""
    run_root = Path(run_root)
    resolved_root = run_root.resolve()
    root_stat = os.stat(resolved_root)
    # the observation snapshot is taken before any scan read and covers the anchor's tree, so an
    # original moved into a prunable tree during the scan keeps its pinned identity and removal
    # refuses it
    anchor_dir = Path(anchor_dir) if anchor_dir is not None else default_anchor_dir(run_root)
    observed = _snapshot_idents(run_root, anchor_dir)
    sentinel = read_sentinel(run_root)
    if sentinel is None:
        # D5, DL-138: this reader does not route through `read_journal`, so it names a retired
        # `header` opening itself; any other content at that path is unknown residue.
        if opens_with_rec(sentinel_path(run_root)) == "header":
            raise EngineError(
                f"{sentinel_path(run_root)}: opens with `header`, a RETIRED record"
                " dialect refused by name since DL-138 -- it never joined a period"
                " lineage, and retention plans over periods"
                " (docs/protocol-evolution.md ss6, ss8)"
            )
        raise EngineError(
            f"{run_root}: no `{SENTINEL_NAME}` sentinel -- retention is a period"
            " model, and this directory holds no estate (period-model ss1.1)"
        )
    stored = EstateAnchor(anchor_dir).read()
    if stored is None:
        raise EngineError(
            f"{anchor_dir}: no anchor -- the lineage head is what says which"
            " artifacts are reachable, and nothing may be pruned without it"
            " (period-model ss1.3, ss12)"
        )
    if stored.estate_id != sentinel.estate_id:
        raise EngineError(
            f"{anchor_dir}: anchor of estate {stored.estate_id} against a root"
            f" whose sentinel says {sentinel.estate_id} -- refusing to prune one"
            " estate by another's head (period-model ss1.2)"
        )
    segments = wal_segments(run_root)
    periods = _periods_held(run_root, segments)
    registry = _registry_periods(stored, anchor_dir)
    archived = _archive_receipts(run_root, periods, stored)
    _check_no_silent_loss(run_root, registry, segments, archived)
    if not segments and not archived:
        raise EngineError(
            f"{run_root}: the sentinel names `wal/` and this root holds no segment"
            " and no archive receipt -- an interrupted genesis, not an estate to"
            " prune (period-model ss1.1)"
        )
    if not segments:
        # every segment was archived: an old root of a rolled lineage. Its sidecars,
        # attestations and receipts are a permanent floor, so the plan is still computed.
        current = max(periods)
    else:
        current = segments[-1]
    attested = _attested_periods(run_root, periods)
    seals = _seals_held(run_root, periods, estate_id=sentinel.estate_id)
    born = _spawn_periods(run_root, segments)
    scan = _Scan(
        run_root=run_root,
        anchor_dir=anchor_dir,
        segments=segments,
        periods=periods,
        current=current,
        attested=attested,
        seals=seals,
        opening=_opening_period(run_root, current) if segments else None,
        born=born,
        archived=archived,
        archive={},
        head=max((number for number, _ in registry), default=current),
        covered_through=_covered_through(sentinel.estate_id, registry),
    )
    scan = replace(scan, archive=_archive_eligibility(scan))
    artifacts: list[Artifact] = []
    artifacts += _lineage_artifacts(scan, stored.head)
    artifacts += _period_artifacts(scan)
    artifacts += _staging_artifacts(scan)
    artifacts += _spool_artifacts(scan)
    stamped = [
        replace(
            item,
            ident=_snapshot_stamp(observed, item.path),
            licensed=frozenset(_observed_under(observed, run_root, item.path)),
        )
        if item.verdict == "prunable"
        else item
        for item in artifacts
    ]
    retained_idents = frozenset(
        ident
        for item in artifacts
        if item.verdict != "prunable"
        for ident in _observed_under(observed, run_root, item.path)
    )
    # the bracket: every retained top-level path must still hold the inode the snapshot
    # observed, or a substitution during the scan would pin the replacement
    for item in artifacts:
        if item.verdict == "prunable":
            continue
        pinned = observed.get(item.path)
        now = _lstat_ident(item.path)
        if pinned is not None and now != pinned:
            raise EngineError(
                f"{item.path}: changed identity while the plan was being computed --"
                " the estate is being mutated under the planner, re-plan"
                " (period-model ss12)"
            )
    return RetentionPlan(
        run_root=run_root,
        anchor_dir=anchor_dir,
        estate_id=stored.estate_id,
        root_ident=(root_stat.st_dev, root_stat.st_ino),
        retained_idents=retained_idents,
        current_period=current,
        attested=frozenset(attested),
        archived=frozenset(archived),
        artifacts=tuple(sorted(stamped, key=lambda item: (str(item.path), item.kind))),
    )


def _snapshot_idents(*roots: Path) -> dict[Path, tuple[int, int]]:
    """Every path under `roots` with its inode, taken from the directory enumeration itself
    (`scandir` on an open descriptor), so no pathname lookup can be swapped.

    Descent opens each child with O_NOFOLLOW and requires its fstat to equal the listed (device,
    d_ino). Fail-closed on every error."""
    out: dict[Path, tuple[int, int]] = {}

    def _refuse(where: Path, error: Exception) -> None:
        raise EngineError(
            f"{where}: estate unreadable or mutating ({error}) -- retention refuses"
            " to plan over a tree it cannot fully observe (period-model ss12)"
        ) from error

    def _walk(fd: int, shown: Path) -> None:
        st = os.fstat(fd)
        out[shown] = (st.st_dev, st.st_ino)
        try:
            entries = list(os.scandir(fd))
        except OSError as exc:
            _refuse(shown, exc)
            raise AssertionError("unreachable") from exc
        for entry in entries:
            child_shown = shown / entry.name
            listed = (st.st_dev, entry.inode())
            out[child_shown] = listed
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                _refuse(child_shown, exc)
                raise AssertionError("unreachable") from exc
            if not is_dir:
                continue
            try:
                child_fd = os.open(
                    entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            except OSError as exc:
                _refuse(child_shown, exc)
                raise AssertionError("unreachable") from exc
            try:
                cst = os.fstat(child_fd)
                if (cst.st_dev, cst.st_ino) != listed:
                    _refuse(
                        child_shown,
                        OSError("directory identity changed between listing and descent"),
                    )
                _walk(child_fd, child_shown)
            finally:
                os.close(child_fd)

    for root in roots:
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            continue  # an anchor directory need not exist
        except OSError as exc:
            _refuse(root, exc)
            raise AssertionError("unreachable") from exc
        try:
            _walk(fd, root)
        finally:
            os.close(fd)
    return out


def _observed_under(
    observed: Mapping[Path, tuple[int, int]], run_root: Path, path: Path
) -> list[tuple[int, int]]:
    """The snapshot's identities at and beneath `path`.

    A retained path the snapshot never saw was born mid-scan and is pinned through the live
    traversal."""
    if path not in observed:
        # born mid-scan or on a layout the snapshot lacks (an anchor not yet created): no
        # pre-scan swap could target it
        return _idents_under(path)
    prefix = str(path)
    return [
        ident
        for seen, ident in observed.items()
        if seen == path or str(seen).startswith(prefix + os.sep)
    ]


def _idents_under(path: Path) -> list[tuple[int, int]]:
    """Every inode at and beneath `path`, links never followed.

    Fail-closed: an unreadable entry inside a retained tree refuses the plan, since skipping it
    would leave its children unpinned."""
    out: list[tuple[int, int]] = []
    try:
        top = os.lstat(path)
    except OSError as exc:
        # the scan observed this artifact moments earlier, so absence, at the top level too, is
        # concurrent mutation; a rename into a prunable tree looks like an unlink
        raise EngineError(
            f"{path}: retained artifact unreadable ({exc}) -- retention refuses to"
            " plan over a tree it cannot fully pin (period-model ss12)"
        ) from exc
    out.append((top.st_dev, top.st_ino))
    if not stat_module.S_ISDIR(top.st_mode):
        return out

    def _refuse(error: OSError) -> None:
        raise EngineError(
            f"{error.filename}: retained tree unreadable ({error}) -- retention"
            " refuses to plan over a tree it cannot fully pin (period-model ss12)"
        ) from error

    for base, dirs, files in os.walk(path, onerror=_refuse, followlinks=False):
        for name in dirs + files:
            try:
                st = os.lstat(Path(base) / name)
            except OSError as exc:
                # a vanish between listing and lstat cannot be told from a rename into a
                # prunable tree, so fail closed
                _refuse(exc)
                raise AssertionError("unreachable") from exc
            out.append((st.st_dev, st.st_ino))
    return out


def _snapshot_stamp(observed: Mapping[Path, tuple[int, int]], path: Path) -> tuple[int, int] | None:
    """A prunable artifact's identity from the snapshot, only while the disk still agrees.

    None refuses the removal, so one changed artifact does not block the sweep."""
    pinned = observed.get(path)
    if pinned is None:
        return None  # never observed: nothing licensed ITS deletion
    return pinned if _lstat_ident(path) == pinned else None


def _lstat_ident(path: Path) -> tuple[int, int] | None:
    """The artifact's own (st_dev, st_ino), never following a symlink; None when unreadable."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


@dataclass(frozen=True)
class _Scan:
    """What every rule reads, gathered once, so no rule re-reads the disk and disagrees with
    another about which periods this root holds."""

    run_root: Path
    anchor_dir: Path
    segments: list[int]
    #: every period this root holds anything for: segments plus `periods/<N>/` installed by a
    #: committed seal before its segment exists
    periods: list[int]
    current: int
    attested: set[int]
    seals: dict[int, Seal]
    #: the period the current segment opened from (its `opens_from_seal`); null when the current
    #: period is 1
    opening: int | None
    #: `(job, run_number) -> (birth period, run_id)`, read once so the archive's spool-order
    #: dependency and the tombstone floor agree
    born: dict[tuple[str, int], tuple[int, Any]]
    #: every period this root holds an archive receipt for (ss12, DL-144)
    archived: dict[int, ArchiveReceipt]
    #: `period -> None when its inputs may be archived, else why not`; `held` reports the string
    archive: dict[int, str | None]
    #: the estate's head period: the highest period ss1.3's registry names, whatever root holds
    #: it; not this root's newest segment once a lineage has rolled
    head: int
    #: the highest period whose attestation verifies anywhere in the estate, or None. One
    #: checkpoint covers every period below it by induction; a per-root answer would floor a
    #: rolled root's last period forever.
    covered_through: int | None


def _periods_held(run_root: Path, segments: Sequence[int]) -> list[int]:
    """Every period number this root holds an artifact for.

    Segments alone miss two cases: a committed seal installs `periods/N+1/manifest.json` before
    the successor segment exists (ss12 floors it), and a rolled root holds the seal it opened
    from and its attestation but none of that period's WAL (ss1.3)."""
    held = set(segments)
    for directory in (run_root / "periods", seal_dir(run_root)):
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            # `000002/`, `000002.json` and `000002.audit.json` all name
            # period 2; the first dot is where the number ends
            stem = entry.name.split(".", 1)[0]
            if len(stem) == 6 and stem.isdigit():
                held.add(int(stem))
    return sorted(held)


def _archive_receipts(
    run_root: Path, periods: Sequence[int], stored: Anchor
) -> dict[int, ArchiveReceipt]:
    """Every archive receipt this root holds, proved before it is believed (ss12, DL-144).

    `attest.verify_archive_receipt` is the one door, shared with the walk, the tier, `journal` and
    `runs`. This adds the anchor: the verifier binds a receipt to the root's sentinel, and the
    plan's anchor must name the same estate."""
    out: dict[int, ArchiveReceipt] = {}
    for period_id in periods:
        receipt = verify_archive_receipt(run_root, period_id)
        if receipt is None:
            continue
        # unreachable unless the sentinel is rewritten in place between `plan_retention`'s read
        # and the verifier's read; no dsl41 writer does that. It also guards the removal of
        # either gate
        if receipt.estate_id != stored.estate_id:  # pragma: no cover -- see above
            raise EngineError(
                f"{archive_receipt_path(run_root, period_id)}: estate"
                f" {receipt.estate_id} under an anchor naming {stored.estate_id} -- a"
                " stranger's receipt excuses nothing here (period-model ss12)"
            )
        out[period_id] = receipt
    return out


def _registry_periods(stored: Anchor, anchor_dir: Path) -> list[tuple[int, PeriodRow]]:
    """ss1.3's archive registry as `(period, row)`, ascending, provisional rows dropped.

    The whole row is kept: `seal_digest` is what the lineage committed for the period, and a
    cover that read only the path could be sent to a same-estate root holding another branch's
    seal (DL-144). `boundary.registry_rows` owns the read and refuses the two ways a registry can
    be wrong (DL-145)."""
    return [
        (period_id, row)
        for period_id, row in registry_rows(stored, anchor_dir=anchor_dir)
        if row.segment_durable
    ]


def _covered_through(estate_id: str, registry: Sequence[tuple[int, PeriodRow]]) -> int | None:
    """The highest period whose attestation verifies on a root this estate proves, or None
    (ss1.3's induction, DL-144).

    An estate question, and the one place this module reads outside the root it plans: a rolled
    root's last period is covered by a checkpoint the successor root holds, so a per-root answer
    would floor it forever.

    The row says where to look and which seal is the lineage's. One unproved artifact must never
    authorize a deletion, so four bindings stand between a row and a cover:

    1. the root carries a `period_root` sentinel of this estate;
    2. the sidecar under that period's number is this estate's and that period's (`read_seal`
       parses a sidecar and never asks whose);
    3. its digest is the one ss1.3's row committed for that period, which makes the branch the
       lineage's;
    4. `verify_attestation` binds the checkpoint to that sidecar and to its own
       `chain_through_period`.

    A missing or unreadable root is skipped (an off-line root only ever holds more). A present
    root that fails a binding refuses: that is an edited row. Rows are read descending and
    stopped at the first verifying checkpoint, which covers every period below it."""
    for period_id, row in reversed(registry):
        root = Path(row.root)
        if not root.is_dir():
            continue  # off-line, archived away: proves nothing, releases nothing
        sentinel = read_sentinel(root)
        if sentinel is None or sentinel.estate_id != estate_id:
            raise EngineError(
                f"{root}: registered for period {period_id} of estate {estate_id} and"
                f" it belongs to {'no lineage' if sentinel is None else sentinel.estate_id}"
                " -- a registry row that resolves to a stranger's root proves nothing"
                " here, and retention refuses to plan while one authorizes a deletion"
                " (period-model ss1.2, ss1.3, ss12)"
            )
        if row.seal_digest is None or not attestation_path(root, period_id).exists():
            # an unsealed period, or one this root holds no checkpoint for, covers nothing
            continue
        try:
            seal = read_seal(root, period_id)
        except (OSError, EngineError):
            continue  # a sidecar that will not parse binds no checkpoint
        if seal.estate_id != estate_id or seal.period_id != period_id:
            raise EngineError(
                f"{seal_path(root, period_id)}: attests period {seal.period_id} of"
                f" estate {seal.estate_id} where this lineage expects period"
                f" {period_id} of {estate_id} -- a foreign pair in a registered root"
                " (period-model ss1.2, ss12)"
            )
        if row.seal_digest != seal.digest:
            raise EngineError(
                f"{seal_path(root, period_id)} digests to {seal.digest} and ss1.3's"
                f" registry committed {row.seal_digest} for period {period_id} -- a"
                " sidecar of this estate that this LINEAGE never closed with. Another"
                " branch's checkpoint covers another branch's periods, and releasing"
                " a WAL on it would delete evidence of the run that did happen"
                " (period-model ss1.3, ss12)"
            )
        try:
            verify_attestation(root, period_id)
        except (OSError, EngineError):
            # a checkpoint that proves nothing unlocks nothing: keep looking down
            continue
        return period_id
    return None


def _check_no_silent_loss(
    run_root: Path,
    registry: Sequence[tuple[int, PeriodRow]],
    segments: Sequence[int],
    archived: Mapping[int, ArchiveReceipt],
) -> None:
    """ss12's refuse-don't-degrade half: a period this root owns whose WAL is gone with no receipt
    licensing its absence (DL-144).

    Ownership comes from ss1.3's registry alone, since a rolled root legitimately holds a
    predecessor's sidecar and attestation without its WAL. Refusing keeps the tombstone floor
    from being computed over an estate that lost a segment and releasing a replayable SPAWN."""
    resolved = _resolved(run_root)
    held = set(segments)
    for period_id, row in registry:
        if _resolved(Path(row.root)) != resolved or period_id in held:
            continue
        receipt = archived.get(period_id)
        if receipt is not None and receipt.licenses(run_root, wal_path(run_root, period_id)):
            continue
        raise EngineError(
            f"{wal_path(run_root, period_id)}: the registry says this root holds"
            f" period {period_id}'s segment and it is not here. No archive receipt"
            f" licenses its absence (`{archive_receipt_path(run_root, period_id).name}`),"
            " so this is LOSS and not an archive -- retention writes the receipt"
            " before it deletes anything, exactly so the two can be told apart"
            " (period-model ss1.3, ss12)"
        )


def _archive_eligibility(scan: _Scan) -> dict[int, str | None]:
    """DL-144's itemized eligibility, per period: None when the inputs may be archived, else the
    dependency that says not yet.

    1. **behind the head.** The current period's inputs are what every recovery opens from.
    2. **covered by a later chain checkpoint.** ss12 floors the latest checkpoint and everything
       after it; a period is archivable once a checkpoint above it carries the induction past it.
    3. **the spool went first** (PR-36b). The tombstone floor resolves a run directory to a
       period through the SPAWN effect in that period's WAL, so archiving the WAL first would
       floor those tombstones forever. Archiving refuses while any run directory, index entry or
       default log of a run born in the period survives, and names them.
    4. **the archived periods are a prefix of what the root retains.** Segment-spanning readers
       (the subscriber backfill's contiguity proof and adjacency chain, ss11's gap marker at the
       oldest retained record) cannot cross a hole. The sweep deletes ascending.

    An archived period gets no entry: `_licensed_artifacts` answers for it from the receipt."""
    out: dict[int, str | None] = {}
    prefix_ok = True
    for period_id in scan.segments:
        if period_id in scan.archived:
            # an archived period's verdict comes from `_licensed_artifacts`, off the receipt; the
            # prefix stays satisfied
            continue
        why = _archive_blocker(scan, period_id)
        if why is None and not prefix_ok:
            why = (
                "an older period this root retains is not archivable, and the archive"
                " runs oldest-first so the retained segments never grow a hole"
            )
        out[period_id] = why
        if why is not None:
            prefix_ok = False
    return out


def _archive_blocker(scan: _Scan, period_id: int) -> str | None:
    """Conditions 1-3 of `_archive_eligibility`, for one period."""
    if period_id >= scan.head:
        return "the estate's head period: its inputs are what every recovery opens from"
    # the period the current segment opened from is not excluded: ss12 floors its seal sidecar,
    # not a WAL, and resume, the carry and history fold from the sidecar (ss11 step 3). An
    # `opening` guard would floor an in-place lineage's every period forever.
    if period_id not in scan.attested:
        return "unattested: nothing stands in for inputs that were never proved"
    if scan.covered_through is None or scan.covered_through <= period_id:
        return (
            f"no chain checkpoint above period {period_id} covers it -- attest a later period first"
        )
    remaining = _remaining_spool(scan.run_root, scan.born, period_id)
    if remaining:
        shown = ", ".join(str(path) for path in remaining[:4])
        more = f" (+{len(remaining) - 4} more)" if len(remaining) > 4 else ""
        return f"its spool is still on disk and must be pruned first (PR-36b order): {shown}{more}"
    return None


def _remaining_spool(
    run_root: Path, born: Mapping[tuple[str, int], tuple[int, Any]], period_id: int
) -> list[Path]:
    """Every tombstone artifact of a run born in this period that is still on disk, sorted.

    Read from the WAL's own SPAWN effects, the join the tombstone floor uses. One walk (DL-145)
    serves the plan's archive blocker and the receipt-time re-check, which passes a fresh `born`
    to catch a spool that returned after planning."""
    return sorted(
        path
        for (job, run_number), (birth, run_id) in born.items()
        if birth == period_id
        for path in _spool_paths(run_root, job, run_number, run_id)
        if path.exists()
    )


def _attested_periods(run_root: Path, periods: Sequence[int]) -> set[int]:
    """Every period this root holds a checkpoint for that verifies.

    The artifact is the authority, not the registry row: `audit` publishes the checkpoint before
    it sets the row, and a run that could not take the lineage lock leaves a checkpoint with no
    row (`attest.Unattested`). A checkpoint that does not verify counts as absent."""
    attested: set[int] = set()
    for period_id in periods:
        if not attestation_path(run_root, period_id).exists():
            continue
        try:
            verify_attestation(run_root, period_id)
        except EngineError:
            continue  # a checkpoint that proves nothing unlocks nothing
        attested.add(period_id)
    return attested


def _seals_held(run_root: Path, periods: Sequence[int], *, estate_id: str) -> dict[int, Seal]:
    """Every sidecar this root holds, parsed and bound; a period with a segment and no sidecar is
    still open.

    These seals authorize deletion (their `executions` prove a run terminal), so four bindings
    apply, as in audit: the sentinel's estate, the period the filename claims, the WAL's own
    `seal` record, and where a successor segment exists its `opens_from_seal` link."""
    seals: dict[int, Seal] = {}
    for period_id in periods:
        if not seal_path(run_root, period_id).exists():
            continue
        seal = read_seal(run_root, period_id)
        if seal.estate_id != estate_id:
            raise EngineError(
                f"{seal_path(run_root, period_id)}: estate {seal.estate_id} under a"
                f" sentinel naming {estate_id} -- a stranger's sidecar authorizes"
                " nothing here (period-model ss12)"
            )
        if seal.period_id != period_id:
            raise EngineError(
                f"{seal_path(run_root, period_id)}: attests period {seal.period_id}"
                " under another period's filename (period-model ss12)"
            )
        wal = wal_path(run_root, period_id)
        if wal.exists():
            committed = [record for record in read_journal(wal) if record.get("rec") == "seal"]
            if len(committed) != 1:
                # a sidecar with a local WAL lacking its naming record (or holding two) is not a
                # committed boundary, and an unbound sidecar must not authorize a deletion
                # (period-model ss2.2, ss12)
                raise EngineError(
                    f"{seal_path(run_root, period_id)}: the local WAL holds"
                    f" {len(committed)} `seal` record(s) -- a sealed period's WAL ends"
                    " in exactly the record that names this sidecar, and retention"
                    " refuses to plan without it (period-model ss2.2, ss12)"
                )
            check_seal_record(committed[-1])  # the FULL ss2.2 schema first
            check_record_names_sidecar(seal, committed[-1], run_root)
        # a rolled root holds the seal it opened from and none of that period's WAL; its binding
        # is the successor link, the first local segment's `opens_from_seal`
        successor = wal_path(run_root, period_id + 1)
        if successor.exists():
            link = read_journal(successor)[0].get("opens_from_seal")
            if isinstance(link, Mapping) and link.get("digest") != seal.digest:
                raise EngineError(
                    f"{seal_path(run_root, period_id)}: the successor opened from"
                    f" {link.get('digest')!r}, not this sidecar -- a replaced pair"
                    " (period-model ss11, ss12)"
                )
        seals[period_id] = seal
    return seals


def _opening_period(run_root: Path, current: int) -> int | None:
    """The period the current segment opened from, read off its own `opens_from_seal` rather than
    computed as `current - 1`."""
    opening = read_journal(wal_path(run_root, current))[0].get("opens_from_seal")
    if isinstance(opening, Mapping):
        period_id = opening.get("period_id")
        # the false arm is unreachable: `read_journal` checks the first record's schema (DL-138),
        # which makes a present `opens_from_seal` exactly `{period_id: wire int, digest}`
        if is_wire_int(period_id):  # pragma: no branch -- see above
            return period_id
    return None


# ------------------------------------------------- ss12 the head's reach


def _lineage_artifacts(scan: _Scan, head: Head) -> list[Artifact]:
    """The sentinel, the anchor and the active claim (ss12).

    The anchor lies outside every archivable root; `_remove` refuses a path not under the run
    root."""
    out = [
        Artifact(
            path=scan.run_root / SENTINEL_NAME,
            kind="sentinel",
            verdict="floored",
            rule="ss1.1",
            why="the one file that says this root belongs to a lineage",
        ),
        Artifact(
            path=scan.anchor_dir / ANCHOR_NAME,
            kind="anchor",
            verdict="floored",
            rule="ss1.3",
            why="the lineage head and the archive registry",
        ),
        Artifact(
            path=scan.anchor_dir / ANCHOR_LOCK_NAME,
            kind="anchor",
            verdict="floored",
            rule="ss1.3",
            why="the lineage lock: replacing it stops the incumbent",
        ),
    ]
    if isinstance(head, ClaimedHead):
        out.append(
            Artifact(
                path=EstateAnchor(scan.anchor_dir).claim_path(head.claim_id),
                kind="claim",
                verdict="floored",
                rule="ss1.3",
                why=f"the durable claim on {head.target_root}, resumable by claim_id",
            )
        )
    return out


def _period_artifacts(scan: _Scan) -> list[Artifact]:
    """Per-period sidecars, attestations, manifests, candidates, bundles and WALs (ss12, PR-36c)."""
    out: list[Artifact] = []
    latest_attested = max(scan.attested) if scan.attested else None
    floored_bundles: set[str] = set()
    for period_id in scan.periods:
        out += _sidecar_artifact(scan, period_id)
        out += _attestation_artifact(scan, period_id, latest_attested)
        out += _receipt_artifact(scan, period_id)
        out += _manifest_artifacts(scan, period_id, floored_bundles)
        if period_id in scan.archived:
            # archived: the receipt is the recovery authority, so the list comes from it and
            # not from what the disk still looks like
            out += _licensed_artifacts(scan, period_id)
            continue
        out += _candidate_artifacts(scan, period_id, floored_bundles)
        if period_id in scan.segments:
            out.append(_wal_artifact(scan, period_id))
    out += _bundle_artifacts(scan, floored_bundles)
    return out


def _reachable_seal(scan: _Scan, period_id: int) -> str | None:
    """Why the head still reaches this sidecar, or None: the one the current period opened from
    and the one it will close with."""
    if period_id == scan.opening:
        return "the seal the current period opened from"
    if period_id == scan.current and period_id in scan.seals:
        return "the seal this period closed with"
    return None


def _sidecar_artifact(scan: _Scan, period_id: int) -> list[Artifact]:
    path = seal_path(scan.run_root, period_id)
    if not path.exists():
        return []
    reason = _reachable_seal(scan, period_id)
    if reason is not None:
        return [
            Artifact(
                path=path,
                kind="sidecar",
                verdict="floored",
                rule="ss12",
                why=reason,
                period_id=period_id,
            )
        ]
    if period_id in scan.archived:
        return [
            Artifact(
                path=path,
                kind="sidecar",
                verdict="floored",
                rule="DL-144",
                why="this period was archived: its sidecar is a PERMANENT floor",
                period_id=period_id,
            )
        ]
    return [
        Artifact(
            path=path,
            kind="sidecar",
            verdict="held",
            rule="DL-144",
            why=(
                "behind the head, and the OPENING SEAL the next period"
                " re-derives from -- no class licenses removing one"
            ),
            period_id=period_id,
        )
    ]


def _attestation_artifact(
    scan: _Scan, period_id: int, latest_attested: int | None
) -> list[Artifact]:
    path = attestation_path(scan.run_root, period_id)
    if not path.exists():
        return []
    if latest_attested is None or period_id >= latest_attested:
        return [
            Artifact(
                path=path,
                kind="attestation",
                verdict="floored",
                rule="ss12",
                why="the latest chain checkpoint, or one after it",
                period_id=period_id,
            )
        ]
    if period_id in scan.archived:
        return [
            Artifact(
                path=path,
                kind="attestation",
                verdict="floored",
                rule="DL-144",
                why=(
                    "this period was archived: this checkpoint IS what stands in for"
                    " its inputs, and is a PERMANENT floor"
                ),
                period_id=period_id,
            )
        ]
    return [
        Artifact(
            path=path,
            kind="attestation",
            verdict="held",
            rule="DL-144",
            why=(
                f"checkpoint {latest_attested} covers it by induction; no class"
                " licenses removing a checkpoint"
            ),
            period_id=period_id,
        )
    ]


def _licensed_artifacts(scan: _Scan, period_id: int) -> list[Artifact]:
    """Every path an archived period's receipt licensed that is still on disk (ss12a, DL-144).

    Enumerated from the receipt alone: after a crash between deleting `candidate.json` and
    `staged_manifest.json` the ordinary derivation sees no candidate, and no plan would list the
    staged file. The verdict is `prunable` unconditionally, since the point of no return is
    behind the period; eligibility is not re-litigated."""
    receipt = scan.archived[period_id]
    out: list[Artifact] = []
    for relative in receipt.archived:
        path = scan.run_root / relative
        if not path.exists():
            continue  # already gone: the archive finished this one
        out.append(
            Artifact(
                path=path,
                kind="wal" if relative.startswith(f"{WAL_DIR}/") else "candidate",
                verdict="prunable",
                rule="DL-144",
                why="the receipt is durable; completing the archive",
                period_id=period_id,
            )
        )
    return out


def _receipt_artifact(scan: _Scan, period_id: int) -> list[Artifact]:
    """`seals/<period>.archive.json` -- a permanent floor (ss12, DL-144).

    The receipt tells a reader that this period's missing inputs were archived and not lost;
    without it the same disk reads as loss, which every reader refuses."""
    path = archive_receipt_path(scan.run_root, period_id)
    if period_id not in scan.archived:
        return []
    return [
        Artifact(
            path=path,
            kind="receipt",
            verdict="floored",
            rule="DL-144",
            why="the archive receipt: without it this period's absence reads as loss",
            period_id=period_id,
        )
    ]


def _committed_next(scan: _Scan) -> int | None:
    """The period the current seal committed the opening of, if any. That manifest is installed
    and authoritative before any segment exists for it, so the head reaches it (ss12)."""
    seal = scan.seals.get(scan.current)
    return None if seal is None else seal.next_period.period_id


def _manifest_artifacts(scan: _Scan, period_id: int, bundles: set[str]) -> list[Artifact]:
    out: list[Artifact] = []
    reachable = period_id in (scan.current, _committed_next(scan))
    manifest = read_period_manifest(scan.run_root, period_id)
    path = period_dir(scan.run_root, period_id) / "manifest.json"
    if manifest is not None:
        if reachable:
            bundles.add(manifest.source_bundle_hash)
        out.append(
            Artifact(
                path=path,
                kind="manifest",
                verdict="floored" if reachable else "held",
                rule="ss12" if reachable else "DL-144",
                why=(
                    "the current period's pins"
                    if period_id == scan.current
                    else "the committed-next period's pins"
                    if reachable
                    else "behind the head; the archive class does not cover a period"
                    " manifest in v1 -- opening a later period folds against it"
                ),
                period_id=period_id,
            )
        )
    return out


def _candidate_artifacts(scan: _Scan, period_id: int, bundles: set[str]) -> list[Artifact]:
    """`staged_manifest.json` and `candidate.json` beside an installed period manifest (ss7, ss12).

    They stay until the seal that installed them commits, because recovery after an
    install-before-seal crash is decided by those two files. `candidate.json` carries the stage
    digest the rename dropped from the path, so a retry tells its own staging from a stale
    install (PR-30d)."""
    directory = period_dir(scan.run_root, period_id)
    if read_candidate(directory) is None:
        return []
    closing = scan.seals.get(period_id - 1)
    committed = closing is not None and closing.next_period.period_id == period_id
    staged = read_staged_manifest(directory)
    if not committed and staged is not None:
        bundles.add(staged.source_bundle_hash)
    if not committed:
        return [
            Artifact(
                path=directory / name,
                kind="candidate",
                verdict="floored",
                rule="ss12",
                why="recovery after an install-before-seal crash is decided by these two files",
                period_id=period_id,
            )
            for name in (STAGED_MANIFEST_NAME, CANDIDATE_NAME)
        ]
    # committed: recovery does not read them. DL-144 puts them in the archive class under the
    # same cover as this period's WAL, because the archive is one act per period. An archived
    # period never reaches here: `_licensed_artifacts` answers for it, off the receipt.
    blocked = scan.archive.get(period_id, "this root retains no segment for this period")
    if blocked is None:
        return [
            Artifact(
                path=directory / name,
                kind="candidate",
                verdict="prunable",
                rule="DL-144",
                why="the seal that installed it committed, and this period's inputs are archivable",
                period_id=period_id,
            )
            for name in (STAGED_MANIFEST_NAME, CANDIDATE_NAME)
        ]
    return [
        Artifact(
            path=directory / name,
            kind="candidate",
            verdict="held",
            rule="DL-144",
            why=f"the seal that installed it committed, and the archive is blocked: {blocked}",
            period_id=period_id,
        )
        for name in (STAGED_MANIFEST_NAME, CANDIDATE_NAME)
    ]


def _wal_artifact(scan: _Scan, period_id: int) -> Artifact:
    """One segment (I1: segment N is period N).

    Unattested, ss12 floors it. Attested, DL-144's `archive-inputs` class may license it against
    every dependency in `_archive_eligibility`; `held` names the one that is not met."""
    path = wal_path(scan.run_root, period_id)
    # an archived period is always in `attested`: `_archive_receipts` refuses a receipt with no
    # checkpoint that holds together
    if period_id not in scan.attested:
        return Artifact(
            path=path,
            kind="wal",
            verdict="floored",
            rule="ss12",
            why="the WAL of an unattested period",
            period_id=period_id,
        )
    blocked = scan.archive.get(period_id, "this root retains no segment for this period")
    if blocked is not None:
        return Artifact(
            path=path,
            kind="wal",
            verdict="held",
            rule="DL-144",
            why=f"attested, and the archive is blocked: {blocked}",
            period_id=period_id,
        )
    return Artifact(
        path=path,
        kind="wal",
        verdict="prunable",
        rule="DL-144",
        why=(
            "the receipt is durable; completing the archive"
            if period_id in scan.archived
            else "attested, covered by a later checkpoint, spool pruned: archivable"
        ),
        period_id=period_id,
    )


def _bundle_artifacts(scan: _Scan, floored: set[str]) -> list[Artifact]:
    """One entry per content-addressed catalog bundle (ss12).

    A bundle is shared by reference, so the floor is too: a bundle a floored manifest names is
    floored, whatever its period number says."""
    directory = scan.run_root / "catalogs"
    if not directory.is_dir():
        return []
    wanted = {bundle_dir(scan.run_root, digest).name for digest in floored}
    out: list[Artifact] = []
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        reachable = entry.name in wanted
        out.append(
            Artifact(
                path=entry,
                kind="bundle",
                verdict="floored" if reachable else "held",
                rule="ss12" if reachable else "DL-144",
                why=(
                    "the bundle and sources.json a reachable manifest names"
                    if reachable
                    else "no reachable manifest names it; content-addressed bundles are"
                    " OUT of the archive class in v1 -- a bundle is shared by reference"
                    " and its reachability across an archived period is a race"
                ),
            )
        )
    return out


def _staging_artifacts(scan: _Scan) -> list[Artifact]:
    """`periods/.staging/` is floored and `periods/.quarantine/` is not (ss7, ss12).

    Staging holds the bytes a boundary in flight installs, and the install refuses when they are
    gone. A quarantined candidate was displaced by a retry under a different digest, and ss12
    releases it once no recovery references it."""
    out: list[Artifact] = []
    staging = scan.run_root / "periods" / ".staging"
    if staging.is_dir():
        for entry in sorted(staging.iterdir()):
            out.append(
                Artifact(
                    path=entry,
                    kind="staging",
                    verdict="floored",
                    rule="ss7",
                    why="the bytes a boundary in flight installs; the install refuses without them",
                )
            )
    quarantine = scan.run_root / "periods" / ".quarantine"
    if quarantine.is_dir():
        for entry in sorted(quarantine.iterdir()):
            out.append(
                Artifact(
                    path=entry,
                    kind="quarantine",
                    verdict="prunable",
                    rule="ss12",
                    why="a superseded candidate; being quarantined is what says nothing reads it",
                )
            )
    return out


# ------------------------------------------- ss11a the idempotency store


def _spool_artifacts(scan: _Scan) -> list[Artifact]:
    """Run directories, their `.by_run_id` index entries and their default logs (ss11a, ss12,
    PR-36b).

    The floor is a safety rule: "no index entry" means "first application", so deleting an index
    entry or a run directory authorizes a spawn. It lifts only where PR-36b lifts it: the period
    holding the effect is attested and the run is terminal."""
    runs = scan.run_root / "runs"
    if not runs.is_dir():
        return []
    born = scan.born
    carried = _last_carrier(scan)
    out: list[Artifact] = []
    for entry in sorted(runs.iterdir()):
        if entry.name == INDEX_DIR:
            continue
        run = _split_run_dir(entry.name)
        unowned = _not_a_spool(entry, run)
        if run is None or unowned is not None:
            out.append(
                Artifact(
                    path=entry,
                    kind="run",
                    verdict="floored",
                    rule="ss11a",
                    why=unowned or "not named `<job>.<run_number>`: unowned, and left alone",
                )
            )
            continue
        outcome = _run_verdict(scan, run, born, carried)
        out.append(
            Artifact(
                path=entry,
                kind="run",
                verdict=outcome.verdict,
                rule=outcome.rule,
                why=outcome.why,
                period_id=outcome.period_id,
                run=run,
            )
        )
        out += _log_artifacts(scan, run, outcome)
    out += _index_artifacts(scan, born, carried)
    return out


@dataclass(frozen=True)
class RunVerdict:
    """One run's retention verdict, decided once by `_run_verdict` and shared by the spool entry,
    its index entry and its logs."""

    verdict: Verdict
    rule: str
    why: str
    period_id: int | None


def _run_verdict(
    scan: _Scan,
    run: tuple[str, int],
    born: Mapping[tuple[str, int], tuple[int, Any]],
    carried: Mapping[tuple[str, int], int],
) -> RunVerdict:
    """PR-36b's two conditions as one answer: the period holding the SPAWN effect is attested and
    the run is terminal.

    Terminality is read from the seals, not the spool. A run live at boundary L is named in seal
    L's `executions` and ended in period L+1, whose seal proves it is not still running; a run no
    seal names ended in the period that spawned it. That period's attestation is the whole gate:
    a checkpoint requires the predecessor's, so every period below it is covered by induction."""
    bound = born.get(run)
    birth = bound[0] if bound is not None else None
    if bound is not None and bound[1] is None:
        # the ss11 fold permits a null run_id only for a run that never reached an adapter; a
        # tombstone on disk for an unbound run is the only identity evidence there is
        return RunVerdict(
            "floored",
            "ss11a",
            "its SPAWN effect carries no run_id: provenance incomplete",
            bound[0],
        )
    if birth is None:
        return RunVerdict(
            "floored",
            "ss11a",
            "no SPAWN effect in the retained WAL names this run: provenance unknown",
            None,
        )
    last = carried.get(run)
    ended = birth if last is None else last + 1
    if ended not in scan.seals:
        # open here, or closed in a root this one is not: a rolled root holds no successor
        # seal, and "cannot prove it ended" floors like "has not ended"
        return RunVerdict(
            "floored",
            "ss12",
            f"live or carried into period {ended}, and this root holds no seal for it",
            ended,
        )
    if ended not in scan.attested:
        return RunVerdict(
            "floored",
            "ss12",
            f"period {ended} is unattested and its audit needs this spool",
            ended,
        )
    return RunVerdict(
        "prunable",
        "PR-36b",
        f"period {ended} is attested and the run is terminal",
        ended,
    )


def _spawn_periods(
    run_root: Path, segments: Sequence[int]
) -> dict[tuple[str, int], tuple[int, Any]]:
    """`(job, run_number) -> (the period whose WAL holds its SPAWN effect, the run_id it bound)`.

    I1 makes the segment number the period number; the run_id is the join the index entries are
    held to (ss11a's one-to-one pair)."""
    born: dict[tuple[str, int], tuple[int, Any]] = {}
    for period_id in segments:
        for record in read_journal(wal_path(run_root, period_id)):
            if record.get("rec") != "decision":
                continue
            for effect in record.get("effects") or ():
                if not isinstance(effect, Mapping) or effect.get("kind") != "SPAWN":
                    continue
                job, run_number = effect.get("job"), effect.get("run_number")
                run_id = effect.get("run_id")
                malformed = (
                    not isinstance(job, str)
                    or not job
                    or not is_wire_int(run_number)
                    or run_number < 1
                    or not (
                        run_id is None or (isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id))
                    )
                )
                if malformed:
                    # a `true` run_number aliases run 1 and a non-grammar run_id is not an
                    # identity; malformed evidence authorizes nothing (period-model ss11a, ss12)
                    raise EngineError(
                        f"{wal_path(run_root, period_id)}: SPAWN with job"
                        f" {job!r}, run_number {run_number!r}, run_id {run_id!r} --"
                        " malformed identity evidence, retention refuses to plan"
                        " (period-model ss11a, ss12)"
                    )
                assert isinstance(job, str) and isinstance(run_number, int)  # narrowed above
                key = (job, run_number)
                seen = born.get(key)
                if seen is not None and (seen[1] != run_id or seen[0] != period_id):
                    # I2: one (job, run_number) is one run, born once. Keeping the first of
                    # two SPAWNs would compute the tombstone floor for the wrong effect
                    raise EngineError(
                        f"{wal_path(run_root, period_id)}: a second SPAWN for"
                        f" {job}.{run_number} (run_id {run_id!r}; period {seen[0]}"
                        f" already bound {seen[1]!r}) -- retention refuses to plan"
                        " over an estate that violates I2 (period-model ss12)"
                    )
                born[key] = (period_id, run_id)
    return born


def _last_carrier(scan: _Scan) -> dict[tuple[str, int], int]:
    """`(job, run_number) -> the highest period whose seal carried it as a live execution`; a run
    no seal names never crossed a boundary."""
    carried: dict[tuple[str, int], int] = {}
    for period_id, seal in scan.seals.items():
        for execution in seal.executions:
            key = (execution.job, execution.run_number)
            carried[key] = max(carried.get(key, 0), period_id)
    return carried


def _index_artifacts(
    scan: _Scan,
    born: Mapping[tuple[str, int], tuple[int, Any]],
    carried: Mapping[tuple[str, int], int],
) -> list[Artifact]:
    """`runs/.by_run_id/<run_id>` (ss11a).

    Each entry names the `(job, run_number)` it belongs to and takes that run's verdict. An entry
    this binary cannot read is floored: it still names a `run_id`."""
    directory = scan.run_root / "runs" / INDEX_DIR
    if not directory.is_dir():
        return []
    out: list[Artifact] = []
    for entry in sorted(directory.iterdir()):
        parsed = _index_run(entry)
        if parsed is None:
            out.append(
                Artifact(
                    path=entry,
                    kind="run_index",
                    verdict="floored",
                    rule="ss11a",
                    why="an index entry this binary cannot read still names a run_id",
                )
            )
            continue
        run = (parsed[0], parsed[1])
        if parsed[2] != entry.name:
            # the body's own run_id must be the filename, or a rewritten body that keeps the
            # tuple would take the tuple's verdict under another identity
            raise EngineError(
                f"{entry}: body says run_id {parsed[2]!r} under filename"
                f" {entry.name} -- the ss11a entry disagrees with itself and"
                " retention refuses to plan over it (period-model ss11a, ss12)"
            )
        bound = born.get(run)
        if bound is not None and bound[1] is not None and bound[1] != entry.name:
            # ss11a's pair is one-to-one both ways: the join is the WAL's own effect, not the
            # body's claim
            raise EngineError(
                f"{entry}: names {run[0]}.{run[1]}, whose SPAWN effect bound run_id"
                f" {bound[1]!r}, not this filename -- the ss11a pair is broken and"
                " retention refuses to plan over it (period-model ss11a, ss12)"
            )
        outcome = _run_verdict(scan, run, born, carried)
        out.append(
            Artifact(
                path=entry,
                kind="run_index",
                verdict=outcome.verdict,
                rule=outcome.rule,
                why=outcome.why,
                period_id=outcome.period_id,
                run=run,
            )
        )
    return out


def _index_run(path: Path) -> tuple[str, int, str] | None:
    from dsl41.canon import CanonError, decode, require_artifact_version

    try:
        payload = decode(path.read_bytes())
    except (OSError, CanonError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        require_artifact_version(payload)
    except CanonError:
        return None  # unversioned or foreign-version evidence: unreadable, floored
    job, run_number = payload.get("job"), payload.get("run_number")
    run_id = payload.get("run_id")
    if isinstance(job, str) and is_wire_int(run_number) and isinstance(run_id, str):
        return (job, run_number, run_id)
    return None


def _log_artifacts(scan: _Scan, run: tuple[str, int], outcome: RunVerdict) -> list[Artifact]:
    """`logs/<job>.<run_number>.out` and `.err`, when they are where the default resolver puts
    them.

    A job that set `std_out_file` logs to a file of its own, which nothing here touches. The logs
    take the run's verdict. Names come from `runner_adapters.default_log_paths`, since this
    module never loads the catalog."""
    from dsl41.runner_adapters import default_log_paths

    out: list[Artifact] = []
    for path in default_log_paths(run[0], run[1], scan.run_root):
        if path.exists():
            out.append(
                Artifact(
                    path=path,
                    kind="run_log",
                    verdict=outcome.verdict,
                    rule=outcome.rule,
                    why=outcome.why,
                    period_id=outcome.period_id,
                    run=run,
                )
            )
    return out


def _not_a_spool(entry: Path, run: tuple[str, int] | None) -> str | None:
    """Why this entry under `runs/` is not a spool this estate wrote, or None when it is one.

    A symlink is refused whatever it points at: the plan reads a name and the removal follows a
    link, so the two would not be the same object."""
    if entry.is_symlink():
        return "a symlink, not a spool directory this estate wrote"
    if run is None:
        return "not named `<job>.<run_number>`: unowned, and left alone"
    if not entry.is_dir():
        return "named like a spool and is not a directory: unowned, and left alone"
    return None


def _split_run_dir(name: str) -> tuple[str, int] | None:
    return split_run_dir(name)  # one parser (period.py, DL-137)


# ------------------------------------------------------------- the verb


def prune(
    plan: RetentionPlan,
    *,
    classes: Iterable[str] = (),
    dry_run: bool = True,
    older_than_days: float | None = None,
    keep_runs: int = 0,
) -> PruneReport:
    """Remove the plan's prunable artifacts that the operator's flags select, and report the rest.

    Policy is the caller's: `classes`, `older_than_days` and `keep_runs` state which licensed
    deletions to perform. Floors cannot be reached from here.

    A removal the operating system refuses (a permission, a vanished directory, a name that is
    not what the plan read) is recorded and the sweep continues, so the report says which
    artifacts went and which did not. A removal the plan refuses still raises: that is a floor
    being reached."""
    unknown = sorted(set(classes) - set(CLASSES))
    if unknown:
        raise EngineError(
            f"unknown retention class(es) {', '.join(unknown)}:"
            f" this verb knows {', '.join(sorted(CLASSES))}"
        )
    selected = _selected(
        plan, classes=classes, older_than_days=older_than_days, keep_runs=keep_runs
    )
    refused: list[tuple[Artifact, str]] = []
    if ARCHIVE_CLASS in set(classes):
        # the point of no return and the eligibility re-check in front of it live here, not in
        # the plan: the plan is pure and this writes. A receipt is durable before the first
        # deletion of the period it licenses, and the re-check runs against the live disk
        selected, refused = _archive_receipts_first(plan, selected, dry_run=dry_run)
    # the refused ones count as chosen: the operator named their class, so they are not
    # "prunable, outside the flags given"
    chosen = {(str(item.path), item.kind) for item in [*selected, *(i for i, _ in refused)]}
    removed: list[Artifact] = []
    failed: list[tuple[Artifact, str]] = []
    wedged: set[tuple[str, int]] = set()
    #: the lowest period whose archived WAL could not be removed. Every later one stays too, so
    #: the retained segments stay a contiguous suffix at every instant
    stuck: int | None = None
    bytes_removed = 0
    for item in selected:
        size = _size_of(item.path)
        if dry_run:
            bytes_removed += size
            continue
        if item.kind == "wal" and stuck is not None:
            failed.append(
                (
                    item,
                    f"period {stuck}'s segment could not be removed, and the archive"
                    " deletes oldest-first so the retained segments never grow a hole",
                )
            )
            continue
        if item.run is not None and item.run in wedged:
            # one artifact of this run already refused, so the rest stays too: a directory kept
            # while its index entry went would break the ss11a pair in the direction that
            # authorizes a spawn
            failed.append((item, f"{item.run[0]}.{item.run[1]} could not be removed whole"))
            continue
        try:
            _remove(plan, item)
        except (OSError, ArtifactChanged) as exc:
            failed.append((item, str(exc)))
            if item.run is not None:
                wedged.add(item.run)
            if item.kind == "wal" and item.period_id is not None:
                stuck = item.period_id if stuck is None else min(stuck, item.period_id)
            continue
        bytes_removed += size
        removed.append(item)
    return PruneReport(
        removed=tuple(selected if dry_run else removed),
        kept=tuple(item for item in plan.prunable() if (str(item.path), item.kind) not in chosen),
        floored=plan.floors(),
        held=plan.held(),
        failed=tuple(failed),
        refused=tuple(refused),
        bytes_removed=bytes_removed,
        dry_run=dry_run,
    )


def _archive_receipts_first(
    plan: RetentionPlan, selected: list[Artifact], *, dry_run: bool
) -> tuple[list[Artifact], list[tuple[Artifact, str]]]:
    """DL-144's point of no return: for every period whose inputs this sweep would delete,
    re-check eligibility and write the receipt durably, before the first deletion of that period.

    Periods are handled ascending, as is the deletion that follows, so the retained segments are
    a contiguous suffix at every instant and no crash window leaves a hole.

    The re-check is independent of the plan and reads the live disk: the period's attestation,
    the covering checkpoint's attestation, the spool of every run born in the period, and the
    archived prefix. A period whose cover was questioned since the plan refuses and is dropped
    from the sweep with its reason. Periods below it still go; periods above it refuse too,
    naming the one below, since archiving them would open the hole the prefix rule prevents.

    A period that already has a receipt writes nothing; a retry completes its deletion."""
    kept: list[Artifact] = []
    refused: list[tuple[Artifact, str]] = []
    by_period: dict[int, list[Artifact]] = {}
    for item in selected:
        if item.kind in CLASSES[ARCHIVE_CLASS] and item.period_id is not None:
            by_period.setdefault(item.period_id, []).append(item)
        else:
            kept.append(item)
    # "archived" means the receipt is durable, not that the file is gone: the prefix condition is
    # asked against receipts as the sweep walks up, since every deletion still lies ahead of
    # every receipt
    done = set(plan.archived)
    for period_id in sorted(by_period):
        items = by_period[period_id]
        # the receipt is re-read from disk and proved through the same door as every other
        # consumer: a sweep whose plan predates another sweep's receipt loses a lock-free race,
        # and this path decides that deletions may proceed
        stored = verify_archive_receipt(plan.run_root, period_id)
        if stored is not None:
            done.add(period_id)
            unlicensed = [item for item in items if not stored.licenses(plan.run_root, item.path)]
            if unlicensed:
                kept += [item for item in items if item not in unlicensed]
                refused += [
                    (
                        item,
                        f"{archive_receipt_path(plan.run_root, period_id)} already"
                        f" licensed period {period_id} and does not name this artifact"
                        " -- the stored list is what a retry completes from, and this"
                        " sweep is not that retry (period-model ss12)",
                    )
                    for item in unlicensed
                ]
                continue
            kept += items
            continue
        blocker = _recheck_archive(plan, period_id, done)
        if blocker is not None:
            refused += [(item, blocker) for item in items]
            continue
        if dry_run:
            done.add(period_id)
            kept += items
            continue
        try:
            _write_receipt(plan, period_id, items)
        except (OSError, EngineError) as exc:
            refused += [(item, str(exc)) for item in items]
            continue
        done.add(period_id)
        kept += items
    return kept, refused


def _recheck_archive(plan: RetentionPlan, period_id: int, done: set[int]) -> str | None:
    """`_archive_eligibility`'s conditions, asked again of the live disk one moment before the
    receipt is written (DL-144).

    It reads the disk, not the plan: re-reading the plan would only prove the plan is unchanged."""
    if not wal_path(plan.run_root, period_id).exists():
        # gone since the plan, and no receipt licensed it (the caller reads one first): not this
        # sweep's to archive; a re-plan meets the absence as loss
        return (
            f"{wal_path(plan.run_root, period_id)} is gone since the plan was computed"
            " and no receipt licenses its absence -- re-plan (period-model ss12)"
        )
    try:
        verify_attestation(plan.run_root, period_id)
    except (OSError, EngineError) as exc:
        return f"period {period_id}'s own attestation no longer verifies ({exc})"
    # the same estate-spine check as the plan's, over the live anchor and not a snapshot: a row
    # redirected since the plan would otherwise supply the cover that licenses this deletion
    # (DL-144)
    stored = EstateAnchor(plan.anchor_dir).read()
    if stored is None or stored.estate_id != plan.estate_id:
        return (
            f"{plan.anchor_dir}: the lineage head is gone or names another estate since"
            " the plan was computed -- nothing may be archived without it"
            " (period-model ss1.3)"
        )
    try:
        covered = _covered_through(plan.estate_id, _registry_periods(stored, plan.anchor_dir))
    except EngineError as exc:
        return str(exc)
    if covered is None or covered <= period_id:
        return (
            f"no chain checkpoint above period {period_id} covers it any more"
            " -- attest a later period first"
        )
    remaining = _remaining_spool(
        plan.run_root, _spawn_periods(plan.run_root, [period_id]), period_id
    )
    if remaining:
        shown = ", ".join(str(path) for path in remaining[:4])
        return f"its spool is back on disk and must be pruned first (PR-36b order): {shown}"
    older = [
        number
        for number in wal_segments(plan.run_root)
        if number < period_id and number not in done
    ]
    if older:
        return (
            f"this root retains unarchived period(s) {older} below {period_id}, and the"
            " archive runs oldest-first so the retained segments never grow a hole"
        )
    return None


def _spool_paths(run_root: Path, job: str, run_number: int, run_id: Any) -> list[Path]:
    """Every tombstone path one run owns: the directory, the index entry and the default logs.

    One spelling, read by the plan's blocker and the re-check, so both agree on "pruned"."""
    from dsl41.runner_adapters import default_log_paths

    runs = run_root / "runs"
    out = [runs / f"{job}.{run_number}", *default_log_paths(job, run_number, run_root)]
    if isinstance(run_id, str):
        out.append(runs / INDEX_DIR / run_id)
    return out


def _write_receipt(plan: RetentionPlan, period_id: int, items: Sequence[Artifact]) -> None:
    """The receipt for one period, durable before anything of that period is deleted (ss12,
    DL-144).

    `durable_create`, not a write: a receipt already at the name is a previous archive of this
    period, and overwriting it would replace the list a retry completes from. The caller has read
    the stored receipt, so a `FileExistsError` here means two sweeps wrote at once and the loser
    refuses."""
    from datetime import UTC, datetime

    attestation = verify_attestation(plan.run_root, period_id)
    receipt = ArchiveReceipt(
        estate_id=plan.estate_id,
        period_id=period_id,
        seal_digest=attestation.seal_digest,
        attestation_digest=attestation.digest,
        chain_through_period=attestation.chain_through_period,
        retention_class=ARCHIVE_CLASS,
        archived=tuple(sorted({item.path.relative_to(plan.run_root).as_posix() for item in items})),
        archived_at=datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="microseconds"),
        dsl41_version=dsl41_version(),
    )
    write_archive_receipt(plan.run_root, receipt)


def _selected(
    plan: RetentionPlan,
    *,
    classes: Iterable[str],
    older_than_days: float | None,
    keep_runs: int,
) -> list[Artifact]:
    """The prunable artifacts the flags choose, thresholds applied.

    Both thresholds filter runs and carry their index entries and logs with them, so the ss11a
    pair stays one-to-one.

    `keep_runs` is per job, because `run_number` is per job (I2); one list ranked across jobs
    would compare numbers from different series.

    `older_than_days` reads the run's own artifacts, whichever survive, so an orphaned index
    entry beside a hand-deleted directory is judged as that run's."""
    kinds = {kind for name in classes for kind in CLASSES[name]}
    candidates = [item for item in plan.prunable() if item.kind in kinds]
    dropped: set[tuple[str, int]] = set()
    if keep_runs > 0:
        per_job: dict[str, list[int]] = {}
        for item in candidates:
            if item.run is not None:
                per_job.setdefault(item.run[0], []).append(item.run[1])
        for job, numbers in per_job.items():
            for number in sorted(set(numbers), reverse=True)[:keep_runs]:
                dropped.add((job, number))
    if older_than_days is not None:
        cutoff = time.time() - older_than_days * 86400.0
        touched: dict[tuple[str, int], float] = {}
        for item in candidates:
            if item.run is not None:
                touched[item.run] = max(touched.get(item.run, 0.0), _mtime(item.path))
        dropped |= {run for run, when in touched.items() if when > cutoff}
    return [item for item in candidates if item.run is None or item.run not in dropped]


def _mtime(path: Path) -> float:
    """The path's modification time, or `inf` when unreadable.

    `inf`, not 0: this feeds a deletion filter, where an unreadable stat reads as too new to
    touch."""
    try:
        return path.stat().st_mtime
    except OSError:
        return float("inf")


def _remove(plan: RetentionPlan, item: Artifact) -> None:
    """Delete one artifact, after proving the plan allows it (PR-36b, PR-36c).

    Three proofs: the verdict says the spec licenses it; run-root containment says this verb
    cannot reach the anchor, another estate or anything merely pointed at; `retained_over` says
    the path holds no floored or held artifact beneath it."""
    if item.verdict != "prunable":
        raise EngineError(
            f"{item.path}: verdict {item.verdict} ({item.rule}) -- {item.why}:"
            " this verb removes prunable artifacts and nothing else"
            " (period-model ss12, PR-36c)"
        )
    try:
        relative = item.path.relative_to(plan.run_root)
    except ValueError as exc:
        raise EngineError(
            f"{item.path}: not inside {plan.run_root} -- retention prunes one estate"
            " root and never the anchor beside it (period-model ss1.1)"
        ) from exc
    if not relative.parts:
        raise EngineError(f"{item.path}: is the run root itself (period-model ss1.1)")
    if any(part in ("..", ".") for part in relative.parts):
        # `relative_to` is lexical: a `..` would let the descriptor walk leave the proven root
        raise EngineError(
            f"{item.path}: path traverses outside the run root -- refused (period-model ss1.1)"
        )
    blocked = plan.retained_over(item.path)
    if blocked is not None:
        raise EngineError(
            f"{item.path}: would take {blocked.path} with it, which is {blocked.verdict}"
            f" ({blocked.rule}: {blocked.why}) -- refusing (period-model ss12, PR-36c)"
        )
    if item.ident is None:
        raise ArtifactChanged(
            f"{item.path}: the plan holds no identity for this artifact (absent or"
            " changed at observation) -- nothing unverifiable is removed"
            " (period-model ss12)"
        )
    # descriptor-relative from here: the root fd is verified against the plan's inode once, and
    # every step walks openat with O_NOFOLLOW from it, so no later rename or retarget can
    # redirect the deletion
    root_fd = os.open(plan.run_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        live = os.fstat(root_fd)
        if (live.st_dev, live.st_ino) != plan.root_ident:
            raise EngineError(
                f"{plan.run_root}: no longer the directory the plan was computed over"
                " (the root was moved or retargeted) -- re-plan before pruning"
                " (period-model ss12)"
            )
        parent_fd = root_fd
        opened: list[int] = []
        try:
            for part in relative.parts[:-1]:
                parent_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                )
                opened.append(parent_fd)
            name = relative.parts[-1]
            # isolate first, atomically: the name's content moves to a scratch name in the same
            # directory, so no rename can swap it between the identity proof and the deletion.
            # The scratch name is an unpredictable uuid4 checked free first: Python has no
            # portable RENAME_NOREPLACE, and a deterministic name could be pre-seeded with a
            # retained artifact for the rename to destroy
            scratch = f".pruning-{uuid.uuid4().hex}"
            try:
                os.lstat(scratch, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            else:  # pragma: no cover -- a uuid collision
                raise ArtifactChanged(f"{item.path}: scratch name occupied -- re-run")
            try:
                os.rename(name, scratch, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except FileNotFoundError as exc:
                raise ArtifactChanged(
                    f"{item.path}: gone since the plan was computed -- re-plan (period-model ss12)"
                ) from exc
            st = os.lstat(scratch, dir_fd=parent_fd)
            if (st.st_dev, st.st_ino) != item.ident:
                # not the artifact the plan verified. It is not renamed back, since without a
                # no-replace rename a restore can overwrite an entry that appeared at the name;
                # the refusal reports where it stays
                raise ArtifactChanged(
                    f"{item.path}: not the artifact the plan was computed over"
                    " (renamed or replaced since) -- re-plan before pruning; the"
                    f" entry was left at {item.path.parent / scratch}"
                    " (period-model ss12)"
                )
            assert item.licensed is not None  # stamped beside ident; ident gated above
            _remove_at(
                parent_fd,
                scratch,
                retained=plan.retained_idents,
                licensed=item.licensed,
                shown=item.path,
            )
            os.fsync(parent_fd)  # the deletion is a directory-entry write
        finally:
            for fd in opened:
                os.close(fd)
    finally:
        os.close(root_fd)


def _remove_at(
    dir_fd: int,
    name: str,
    *,
    retained: frozenset[tuple[int, int]],
    licensed: frozenset[tuple[int, int]],
    shown: Path,
) -> None:
    """Delete `name` relative to a verified directory fd, never following a symlink or resolving a
    path string.

    Every entry's identity is checked against the plan's retained set first, so a floored
    artifact moved inside a prunable directory refuses. The refusal leaves the partly removed
    tree under its scratch name; the retained artifact is untouched."""
    st = os.lstat(name, dir_fd=dir_fd)
    ident = (st.st_dev, st.st_ino)
    if ident in retained:
        raise ArtifactChanged(
            f"{shown}: a retained artifact was moved beneath this deletion target"
            " since the plan was computed -- refusing (period-model ss12, PR-36c)"
        )
    if ident not in licensed:
        # the inverse licence, per artifact: only what the snapshot saw at and beneath this
        # artifact is licensed
        raise ArtifactChanged(
            f"{shown}: an artifact the plan did not license beneath this deletion"
            " target -- refusing (period-model ss12)"
        )
    if not stat_module.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=dir_fd)
        return
    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        for entry in os.listdir(child_fd):
            _remove_at(child_fd, entry, retained=retained, licensed=licensed, shown=shown)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=dir_fd)


def _resolved(path: Path) -> Path:
    """`normalized_root` as a `Path`: one spelling of "the same root" (DL-145)."""
    return Path(normalized_root(path))


def _size_of(path: Path) -> int:
    if path.is_file():
        return _file_size(path)
    total = 0
    for base, _dirs, names in os.walk(path):
        for name in names:
            total += _file_size(Path(base) / name)
    return total


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
