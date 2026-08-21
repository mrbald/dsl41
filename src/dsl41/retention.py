"""Retention floors, and the verb that prunes what is left (period-model
ss11a, ss12; DL-135).

A retention **policy** is a non-goal (ss12): which periods, spools and
tombstones an estate keeps, and for how long, is a business decision and
lives in `deployment-runbook.md` ss2a as operator flags. What may never be
pruned is the spec's, it is itemized, and this module computes it.

The floor is **everything reachable from the lineage head**: the sentinel;
the anchor and any active claim; the seal sidecar the current period opened
from and the one it will close with; the current and committed-next period
manifests; an installed-but-uncommitted candidate's `staged_manifest.json`
and `candidate.json`; their catalog bundles and `sources.json`; the latest
attestation checkpoint and every attestation after it; the WAL and spool of
any unattested period; the spool of any live or carried execution; and any
SPAWN tombstone whose effect can still be replayed. Recovery refuses
without a sidecar or a catalog directory, so a rule that could delete them
while obeying the tombstone floor was a rule that could delete the only
artifacts able to open the head (PR-36c).

Three verdicts, not two, because ss12 leaves a middle:

- **floored** -- ss12 or ss11a forbids it. `prune` cannot reach it.
- **held** -- the floor has lifted (the head moved past it and a later
  checkpoint covers it) and PR-Q3/E20 is still open, so nothing here
  deletes it. That open question asks whether a seal-only archive may
  stand in for pruned inputs; until it is answered, the inputs of a period
  that must stay auditable stay.
- **prunable** -- the spec licenses deletion by name: a SPAWN tombstone
  whose period is attested and whose run is terminal (PR-36b, ss11a), and
  a quarantined candidate no recovery references (ss12).

The one asymmetry is deliberate and is the spec's. ss12 floors "the WAL and
spool of any unattested period (E20 gates the rest)", which would hold an
attested period's spool too; PR-36b then says of a run directory and its
index entry, in as many words, "after the period is attested and the run
terminal, it may go". The itemized obligation wins for the spool, and the
WAL -- the input E20 is actually about -- is held.

Deleting a floored artifact is structurally impossible through this module:
`prune` iterates the plan's `prunable` items alone, and `_remove` refuses a
path that is not one, that is not under the run root, or that CONTAINS a
floored or held artifact.
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

from dsl41.attest import verify_attestation
from dsl41.boundary import (
    ANCHOR_LOCK_NAME,
    ANCHOR_NAME,
    ClaimedHead,
    EstateAnchor,
    default_anchor_dir,
    read_candidate,
    read_seal,
    read_staged_manifest,
    check_record_names_sidecar,
    check_seal_record,
)
from dsl41.runner_procid import fsync_dir
from dsl41.runner_effects import RUN_ID_RE
from dsl41.period import (
    SENTINEL_NAME,
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
)
from dsl41.runner_clock import EngineError
from dsl41.runner_journal import opens_with_rec, read_journal
from dsl41.seal import Seal

#: the run_id -> (job, run_number) index the supervisor writes FIRST
#: (ss11a). The same name lives in `runner_supervisor._INDEX_DIR`, which
#: this module may not import: that tier imports no dsl41 at all (DL-42).
INDEX_DIR: Final[str] = ".by_run_id"

Verdict = Literal["floored", "held", "prunable"]

#: what an operator's flags select, and the kinds each one covers. The
#: classes exist so a `--dry-run` and a real run report the same items;
#: which of them to remove is the operator's call and never this module's.
CLASSES: Final[dict[str, tuple[str, ...]]] = {
    "tombstones": ("run", "run_index", "run_log"),
    "quarantine": ("quarantine",),
}


class ArtifactChanged(EngineError):
    """The disk no longer holds the artifact the plan verified -- a rename
    or replacement since planning. A DISK fact, not a plan defect: the
    sweep records it and continues, exactly as it does an OS refusal,
    because aborting would leave the operator a traceback and no list."""


@dataclass(frozen=True)
class Artifact:
    """One thing on disk, and what retention says about it.

    `rule` is the citation that decided the verdict, so a report tells an
    operator which sentence to argue with rather than only what happened."""

    path: Path
    kind: str
    verdict: Verdict
    rule: str
    why: str
    period_id: int | None = None
    run: tuple[str, int] | None = None
    #: (st_dev, st_ino) at PLAN time, stamped on prunable artifacts only:
    #: removal verifies it through the held descriptor, so a floored
    #: artifact renamed onto a prunable name after planning is refused
    #: rather than deleted
    ident: tuple[int, int] | None = None
    #: the snapshot identities observed AT AND BENEATH this artifact --
    #: the only inodes its removal is licensed to delete. Per artifact,
    #: not estate-wide: an observed inode from a class the operator did
    #: NOT select, moved inside this tree after planning, must refuse
    licensed: frozenset[tuple[int, int]] | None = None

    def render(self) -> str:
        where = f"period {self.period_id}" if self.period_id is not None else "estate"
        return f"{self.verdict:9} {self.kind:11} {self.path}  [{where}, {self.rule}] {self.why}"


@dataclass(frozen=True)
class RetentionPlan:
    """What `prune` may do to one run root, decided before it does any of
    it. Pure: nothing here writes, unlinks or takes a lock."""

    run_root: Path
    anchor_dir: Path
    estate_id: str
    #: (st_dev, st_ino) of the RESOLVED root at plan time: a run-root
    #: symlink retargeted between the plan and the prune would satisfy
    #: every path-string containment check inside the replacement estate
    root_ident: tuple[int, int]
    #: the identities of every floored and held artifact at plan time: the
    #: recursive removal refuses any of them found BENEATH a deletion
    #: target, so a retained artifact moved inside a prunable directory
    #: after planning is never swept away with it
    retained_idents: frozenset[tuple[int, int]]
    #: the newest period this root holds a segment for (I1: segment N is
    #: period N), which is the period the head is at or just past
    current_period: int
    #: every period whose attestation this root holds AND which verifies
    attested: frozenset[int]
    artifacts: tuple[Artifact, ...]

    def floors(self) -> tuple[Artifact, ...]:
        return tuple(item for item in self.artifacts if item.verdict == "floored")

    def held(self) -> tuple[Artifact, ...]:
        return tuple(item for item in self.artifacts if item.verdict == "held")

    def prunable(self) -> tuple[Artifact, ...]:
        return tuple(item for item in self.artifacts if item.verdict == "prunable")

    def retained_over(self, path: Path) -> Artifact | None:
        """The retained artifact `path` would take with it: itself, or one
        BENEATH it.

        Removing a directory removes what is inside it, so a guard that
        compared paths for equality alone would let `runs/` go while every
        floored tombstone under it was named in this very plan."""
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
    """What one `prune` call did, and what it did not do to everything
    else. `kept` is prunable and outside the flags given; the other two are
    the plan's own verdicts, reported so a run says why each survivor
    survived."""

    removed: tuple[Artifact, ...]
    kept: tuple[Artifact, ...]
    floored: tuple[Artifact, ...]
    held: tuple[Artifact, ...]
    #: selected, licensed, and refused by the OPERATING SYSTEM, with the
    #: reason. A partial sweep has to be reportable
    failed: tuple[tuple[Artifact, str], ...]
    bytes_removed: int
    dry_run: bool


# ------------------------------------------------------------- the plan


def plan_retention(run_root: Path, *, anchor_dir: Path | None = None) -> RetentionPlan:
    """Read one estate root and decide every artifact's verdict (ss12).

    The anchor is READ and never locked. A live engine holds the lineage
    lock for its process lifetime, and a prune that needed the lock could
    only ever run against a stopped estate. Reading is enough because
    attestation is monotone: a period this call sees as attested cannot
    become unattested under it, and everything a live engine can newly
    create belongs to the OPEN period, which is unattested and therefore
    floored."""
    run_root = Path(run_root)
    resolved_root = run_root.resolve()
    root_stat = os.stat(resolved_root)
    # the OBSERVATION SNAPSHOT, taken before any scan read and covering
    # the anchor's tree too: identity is captured when the estate is
    # first observed, so an original moved into a prunable tree during
    # the scan KEEPS its pinned identity and the removal walk refuses it
    # -- a pin taken after the scan would pin whatever replaced it instead
    anchor_dir = Path(anchor_dir) if anchor_dir is not None else default_anchor_dir(run_root)
    observed = _snapshot_idents(run_root, anchor_dir)
    sentinel = read_sentinel(run_root)
    if sentinel is None:
        # D5, DL-138: this reader does NOT route through `read_journal`, so
        # it carries its own tombstone. A retired OPENING is named; anything
        # else at that path is unknown residue and refuses generically --
        # one says the root predates a retirement, the other says nothing
        # here is an estate. `header` is the only recognised opening there
        # ever was besides `segment`, so a file opening with a retired
        # `result` or `effect` record is residue like any other.
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
    if not segments:
        raise EngineError(
            f"{run_root}: the sentinel names `wal/` and this root holds no segment"
            " -- an interrupted genesis, not an estate to prune (period-model ss1.1)"
        )
    current = segments[-1]
    periods = _periods_held(run_root, segments)
    attested = _attested_periods(run_root, periods)
    seals = _seals_held(run_root, periods, estate_id=sentinel.estate_id)
    scan = _Scan(
        run_root=run_root,
        anchor_dir=anchor_dir,
        segments=segments,
        periods=periods,
        current=current,
        attested=attested,
        seals=seals,
        opening=_opening_period(run_root, current),
    )
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
    # the BRACKET: every retained top-level path must still hold the inode
    # the snapshot observed -- a byte-identical substitution during the
    # scan would otherwise pin the replacement while the original drifted
    # into a prunable tree unprotected
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
        artifacts=tuple(sorted(stamped, key=lambda item: (str(item.path), item.kind))),
    )


def _snapshot_idents(*roots: Path) -> dict[Path, tuple[int, int]]:
    """Every path under `roots` and its inode, captured FROM the directory
    enumeration itself: `scandir` on an opened descriptor yields each
    entry's d_ino at read time, so there is no separate pathname lookup a
    swap can slip between. Descending re-proves the child: it is opened
    under the parent's fd with O_NOFOLLOW and its fstat must equal the
    (device, d_ino) the listing reported -- a mismatch (a swap, a mount
    placed mid-tree) refuses. Fail-closed on every error."""
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

    A retained path OUTSIDE the run root (the anchor, a claim) is not in
    the snapshot walk and pins through the live traversal instead; a
    retained path inside the root that the snapshot never saw appeared
    MID-SCAN, and pinning it live is exactly right -- it cannot have been
    the target of a pre-scan swap."""
    if path not in observed:
        # born mid-scan, or on a layout the snapshot legitimately lacks
        # (a not-yet-created anchor): it cannot have been the target of a
        # pre-scan swap, and the live fail-closed traversal pins it
        return _idents_under(path)
    prefix = str(path)
    return [
        ident
        for seen, ident in observed.items()
        if seen == path or str(seen).startswith(prefix + os.sep)
    ]


def _idents_under(path: Path) -> list[tuple[int, int]]:
    """Every inode at and beneath `path`, links never followed: a retained
    DIRECTORY's protection extends to each file inside it, or a single
    file moved out of it into a prunable tree would be swept away while
    the directory's own inode stayed safely pinned.

    FAIL-CLOSED: an unreadable subdirectory or entry inside a retained
    tree refuses the whole plan -- silently skipping it would leave its
    children unpinned, and restoring access later would let them be moved
    into a prunable tree and swept."""
    out: list[tuple[int, int]] = []
    try:
        top = os.lstat(path)
    except OSError as exc:
        # ENOENT included, and at the TOP level too: every artifact on
        # this list was observed on disk by the scan moments earlier, so
        # an absence here is concurrent mutation -- and a rename into a
        # prunable tree is indistinguishable from an unlink
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
                # ENOENT included: the walk LISTED this entry, and a
                # vanish between the listing and the lstat cannot be told
                # apart from a rename into a prunable tree -- an unlinked
                # inode is harmless, a renamed one is live and unpinned,
                # and fail-closed is the only answer that covers both
                _refuse(exc)
                raise AssertionError("unreachable") from exc
            out.append((st.st_dev, st.st_ino))
    return out


def _snapshot_stamp(observed: Mapping[Path, tuple[int, int]], path: Path) -> tuple[int, int] | None:
    """A PRUNABLE artifact's identity, from the observation snapshot and
    only when the disk still agrees with it: a foreign directory swapped
    in during the scan would otherwise be stamped by a late lstat and
    deleted under a verdict computed for something else. None refuses the
    removal (the existing gate), so one changed artifact never blocks the
    rest of the sweep."""
    pinned = observed.get(path)
    if pinned is None:
        return None  # never observed: nothing licensed ITS deletion
    return pinned if _lstat_ident(path) == pinned else None


def _lstat_ident(path: Path) -> tuple[int, int] | None:
    """The artifact's own (st_dev, st_ino), never following a symlink --
    or None when it cannot be read, which removal refuses."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


@dataclass(frozen=True)
class _Scan:
    """What every rule below reads, gathered once. A private aggregate, so
    a rule cannot quietly re-read the disk and disagree with its
    neighbour about which periods this root holds."""

    run_root: Path
    anchor_dir: Path
    #: the periods this root holds a WAL for
    segments: list[int]
    #: every period this root holds ANYTHING for -- segments plus the
    #: `periods/<N>/` a committed seal installed before its segment exists
    periods: list[int]
    current: int
    attested: set[int]
    seals: dict[int, Seal]
    #: the period the CURRENT segment opened from, off its own
    #: `opens_from_seal` -- null on a root whose current period is 1
    opening: int | None


def _periods_held(run_root: Path, segments: Sequence[int]) -> list[int]:
    """Every period number this root holds an artifact for.

    Segments are not the answer on their own, in two directions. A
    committed seal installs `periods/N+1/manifest.json` BEFORE the
    successor segment exists, and ss12 floors that manifest by name. And a
    ROLLED root holds the seal it opened from and its attestation and NONE
    of that period's WAL, by design (ss1.3) -- the two artifacts a second
    roll imports. A walk over segments alone would have no opinion about
    either, and an artifact with no verdict is one the guard cannot
    protect."""
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


def _attested_periods(run_root: Path, periods: Sequence[int]) -> set[int]:
    """Every period this root holds a checkpoint for that VERIFIES.

    The artifact is the authority and the registry row is not: `audit`
    publishes the checkpoint before it sets the row, and a run that could
    not take the lineage lock leaves a durable checkpoint with no row
    (`attest.Unattested`). A checkpoint that does not verify counts as
    absent, which floors more rather than less."""
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
    """Every sidecar this root holds, parsed AND BOUND. A period with a
    segment and no sidecar is simply still open.

    Bound, because these seals AUTHORIZE deletion (their `executions` are
    what proves a run terminal): a valid sidecar-and-attestation pair
    swapped in from another estate verifies internally and names none of
    this estate's live runs. Four bindings, the same spine audit uses:
    the sentinel's estate, the period the filename claims, the WAL's own
    `seal` record, and -- where a successor segment exists -- its
    `opens_from_seal` link."""
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
                # a sidecar with a LOCAL WAL that lacks its naming record
                # (or holds two) is not a committed boundary this lineage
                # wrote -- and an unbound sidecar must not authorize a
                # deletion (period-model ss2.2, ss12)
                raise EngineError(
                    f"{seal_path(run_root, period_id)}: the local WAL holds"
                    f" {len(committed)} `seal` record(s) -- a sealed period's WAL ends"
                    " in exactly the record that names this sidecar, and retention"
                    " refuses to plan without it (period-model ss2.2, ss12)"
                )
            check_seal_record(committed[-1])  # the FULL ss2.2 schema first
            check_record_names_sidecar(seal, committed[-1], run_root)
        # a ROLLED root holds the seal it opened from and none of that
        # period's WAL, by design: its binding is the successor link below
        # -- the first local segment's `opens_from_seal` names it
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
    """The period the current segment opened from, read off the segment's
    own `opens_from_seal` rather than computed as `current - 1`: the
    record is the authority for which seal this period stands on."""
    opening = read_journal(wal_path(run_root, current))[0].get("opens_from_seal")
    if isinstance(opening, Mapping):
        period_id = opening.get("period_id")
        if isinstance(period_id, int) and not isinstance(period_id, bool):
            return period_id
    return None


# ------------------------------------------------- ss12 the head's reach


def _lineage_artifacts(scan: _Scan, head: Any) -> list[Artifact]:
    """The sentinel, the anchor and the active claim (ss12).

    The anchor is outside every archivable root by design, so these three
    are reported and never reachable: `_remove` refuses a path that is not
    under the run root at all."""
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
    """Per-period sidecars, attestations, manifests, candidates, bundles
    and WAL segments (ss12, PR-36c)."""
    out: list[Artifact] = []
    latest_attested = max(scan.attested) if scan.attested else None
    floored_bundles: set[str] = set()
    for period_id in scan.periods:
        out += _sidecar_artifact(scan, period_id)
        out += _attestation_artifact(scan, period_id, latest_attested)
        out += _manifest_artifacts(scan, period_id, floored_bundles)
        if period_id in scan.segments:
            out.append(_wal_artifact(scan, period_id))
    out += _bundle_artifacts(scan, floored_bundles)
    return out


def _reachable_seal(scan: _Scan, period_id: int) -> str | None:
    """Why the head still reaches this sidecar, or None when it does not.

    Two sidecars are reachable: the one the current period OPENED from,
    and the one it will CLOSE with. Everything older is behind the head."""
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
    return [
        Artifact(
            path=path,
            kind="sidecar",
            verdict="held",
            rule="PR-Q3",
            why="behind the head, and an audit input while E20 is open",
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
    return [
        Artifact(
            path=path,
            kind="attestation",
            verdict="held",
            rule="PR-Q3",
            why=f"checkpoint {latest_attested} covers it by induction",
            period_id=period_id,
        )
    ]


def _committed_next(scan: _Scan) -> int | None:
    """The period the current seal committed the opening of, if it
    committed one. That manifest is installed and authoritative before any
    segment exists for it, so the head reaches it (ss12)."""
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
                rule="ss12" if reachable else "PR-Q3",
                why=(
                    "the current period's pins"
                    if period_id == scan.current
                    else "the committed-next period's pins"
                    if reachable
                    else "behind the head, and an audit input while E20 is open"
                ),
                period_id=period_id,
            )
        )
    out += _candidate_artifacts(scan, period_id, bundles)
    return out


def _candidate_artifacts(scan: _Scan, period_id: int, bundles: set[str]) -> list[Artifact]:
    """`staged_manifest.json` and `candidate.json` beside an installed
    period manifest (ss7, ss12).

    They stay until the seal that installed them COMMITS, because recovery
    after an install-before-seal crash is decided by exactly those two
    files. `candidate.json` carries the stage digest the rename dropped
    from the path, so the retry can tell its own staging from a stale
    install (PR-30d)."""
    directory = period_dir(scan.run_root, period_id)
    if read_candidate(directory) is None:
        return []
    closing = scan.seals.get(period_id - 1)
    committed = closing is not None and closing.next_period.period_id == period_id
    staged = read_staged_manifest(directory)
    if not committed and staged is not None:
        bundles.add(staged.source_bundle_hash)
    why = (
        "the seal that installed this candidate has committed"
        if committed
        else "recovery after an install-before-seal crash is decided by these two files"
    )
    return [
        Artifact(
            path=directory / name,
            kind="candidate",
            verdict="held" if committed else "floored",
            rule="PR-Q3" if committed else "ss12",
            why=why,
            period_id=period_id,
        )
        for name in ("staged_manifest.json", "candidate.json")
    ]


def _wal_artifact(scan: _Scan, period_id: int) -> Artifact:
    """One segment (I1: segment N is period N).

    Unattested, ss12 floors it. Attested, the floor is what PR-Q3/E20 is
    the open question ABOUT -- may a seal-only archive stand in for pruned
    inputs -- so it is held rather than offered."""
    # PENDING: E20 -- carried as period-model PR-Q3. Answering it yes makes
    # an attested period's WAL prunable under a class of its own; answering
    # it no makes this floor permanent. Either way the change is here, and
    # is deliberate: the documented default until then is to keep the file
    attested = period_id in scan.attested
    return Artifact(
        path=wal_path(scan.run_root, period_id),
        kind="wal",
        verdict="held" if attested else "floored",
        rule="PR-Q3" if attested else "ss12",
        why=(
            "attested, and the input E20 asks about"
            if attested
            else "the WAL of an unattested period"
        ),
        period_id=period_id,
    )


def _bundle_artifacts(scan: _Scan, floored: set[str]) -> list[Artifact]:
    """One entry per content-addressed catalog bundle (ss12).

    A bundle is shared: a period that reverts to earlier bytes references
    the directory already there. So the floor is by REFERENCE -- a bundle
    a floored manifest names is floored, whatever its period number says."""
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
                rule="ss12" if reachable else "PR-Q3",
                why=(
                    "the bundle and sources.json a reachable manifest names"
                    if reachable
                    else "no reachable manifest names it, and it is an audit input"
                ),
            )
        )
    return out


def _staging_artifacts(scan: _Scan) -> list[Artifact]:
    """`periods/.staging/` is floored and `periods/.quarantine/` is not
    (ss7, ss12).

    Staging holds the bytes a boundary in flight is about to install, and
    the install refuses when they are gone -- `_prepare_install` names a
    retention sweep as the thing that could remove them. A QUARANTINED
    candidate is the opposite case: it was superseded by a retry under a
    different digest, and ss12 releases it the moment no recovery
    references it, which is what being quarantined means."""
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
    """Run directories, their `.by_run_id` index entries and their default
    logs (ss11a, ss12, PR-36b).

    The store's retention floor is a SAFETY rule and not housekeeping: "no
    index entry" means "first application", so deleting an index entry or a
    run directory authorizes a spawn. The floor lifts only where PR-36b
    lifts it -- the period holding the effect is attested and the run is
    terminal."""
    runs = scan.run_root / "runs"
    if not runs.is_dir():
        return []
    born = _spawn_periods(scan)
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
        verdict, rule, why, period_id = _run_verdict(scan, run, born, carried)
        out.append(
            Artifact(
                path=entry,
                kind="run",
                verdict=verdict,
                rule=rule,
                why=why,
                period_id=period_id,
                run=run,
            )
        )
        out += _log_artifacts(scan, run, verdict, rule, why, period_id)
    out += _index_artifacts(scan, born, carried)
    return out


def _run_verdict(
    scan: _Scan,
    run: tuple[str, int],
    born: Mapping[tuple[str, int], tuple[int, Any]],
    carried: Mapping[tuple[str, int], int],
) -> tuple[Verdict, str, str, int | None]:
    """PR-36b's two conditions as one answer: the period holding the SPAWN
    effect is attested, and the run is terminal.

    Terminality is read from the seals, not from the spool. A run live at
    boundary L is named in seal L's `executions`; it ENDED in period L+1,
    and the seal of L+1 is the artifact that proves it is not still
    running. A run no seal names ended inside the period that spawned it.
    Either way one period is the last that can reference the run, and its
    attestation is the whole gate: producing a checkpoint requires the
    predecessor's, so every period below it is covered by induction."""
    bound = born.get(run)
    birth = bound[0] if bound is not None else None
    if bound is not None and bound[1] is None:
        # the ss11 fold permits a null run_id only for a run that provably
        # never reached an adapter -- and whatever left THIS one both
        # unbound and with a tombstone on disk, deleting the tombstone
        # would sever the only identity evidence there is
        return (
            "floored",
            "ss11a",
            "its SPAWN effect carries no run_id: provenance incomplete",
            bound[0],
        )
    if birth is None:
        return (
            "floored",
            "ss11a",
            "no SPAWN effect in the retained WAL names this run: provenance unknown",
            None,
        )
    last = carried.get(run)
    ended = birth if last is None else last + 1
    if ended not in scan.seals:
        # open here, or closed in a root this one is not: a rolled root
        # holds no successor seal by design, and "I cannot prove it ended"
        # is the same answer as "it has not" for a floor
        return (
            "floored",
            "ss12",
            f"live or carried into period {ended}, and this root holds no seal for it",
            ended,
        )
    if ended not in scan.attested:
        return (
            "floored",
            "ss12",
            f"period {ended} is unattested and its audit needs this spool",
            ended,
        )
    return (
        "prunable",
        "PR-36b",
        f"period {ended} is attested and the run is terminal",
        ended,
    )


def _spawn_periods(scan: _Scan) -> dict[tuple[str, int], tuple[int, Any]]:
    """`(job, run_number) -> (the period whose WAL holds its SPAWN effect,
    the run_id that effect bound)`.

    I1 makes the segment number the period number, so the file the effect
    was read out of IS the answer; the run_id rides along because it is
    the join the index entries are held to (ss11a's one-to-one pair)."""
    born: dict[tuple[str, int], tuple[int, Any]] = {}
    for period_id in scan.segments:
        for record in read_journal(wal_path(scan.run_root, period_id)):
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
                    or isinstance(run_number, bool)
                    or not isinstance(run_number, int)
                    or run_number < 1
                    or not (
                        run_id is None or (isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id))
                    )
                )
                if malformed:
                    # a `true` run_number aliases run 1; a non-grammar
                    # run_id is not an identity. Malformed evidence
                    # authorizes nothing and the plan refuses to be
                    # computed over it (period-model ss11a, ss12)
                    raise EngineError(
                        f"{wal_path(scan.run_root, period_id)}: SPAWN with job"
                        f" {job!r}, run_number {run_number!r}, run_id {run_id!r} --"
                        " malformed identity evidence, retention refuses to plan"
                        " (period-model ss11a, ss12)"
                    )
                assert isinstance(job, str) and isinstance(run_number, int)  # narrowed above
                key = (job, run_number)
                seen = born.get(key)
                if seen is not None and (seen[1] != run_id or seen[0] != period_id):
                    # I2: one (job, run_number) is one run, born once. A
                    # second SPAWN for it -- another period, another
                    # run_id -- is a corrupt estate, and a planner that
                    # silently kept the FIRST would compute the tombstone
                    # floor for the wrong effect and prune a replayable one
                    raise EngineError(
                        f"{wal_path(scan.run_root, period_id)}: a second SPAWN for"
                        f" {job}.{run_number} (run_id {run_id!r}; period {seen[0]}"
                        f" already bound {seen[1]!r}) -- retention refuses to plan"
                        " over an estate that violates I2 (period-model ss12)"
                    )
                born[key] = (period_id, run_id)
    return born


def _last_carrier(scan: _Scan) -> dict[tuple[str, int], int]:
    """`(job, run_number) -> the highest period whose seal carried it as a
    live execution`. A run named by no seal never crossed a boundary."""
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

    Each entry names the `(job, run_number)` it belongs to, so it takes
    that run's verdict. An entry this binary cannot read is floored: it
    still names a `run_id`, and deleting it authorizes a spawn."""
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
            # the body's own run_id must BE the filename: a rewritten body
            # that keeps the tuple would take the tuple's verdict while
            # the entry's identity evidence says something else entirely
            raise EngineError(
                f"{entry}: body says run_id {parsed[2]!r} under filename"
                f" {entry.name} -- the ss11a entry disagrees with itself and"
                " retention refuses to plan over it (period-model ss11a, ss12)"
            )
        bound = born.get(run)
        if bound is not None and bound[1] is not None and bound[1] != entry.name:
            # ss11a's pair is one-to-one BOTH ways: an index body rewritten
            # to name a terminal run would take that run's prunable verdict
            # while the filename still binds a LIVE run's identity -- the
            # join is the WAL's own effect, not the body's claim
            raise EngineError(
                f"{entry}: names {run[0]}.{run[1]}, whose SPAWN effect bound run_id"
                f" {bound[1]!r}, not this filename -- the ss11a pair is broken and"
                " retention refuses to plan over it (period-model ss11a, ss12)"
            )
        verdict, rule, why, period_id = _run_verdict(scan, run, born, carried)
        out.append(
            Artifact(
                path=entry,
                kind="run_index",
                verdict=verdict,
                rule=rule,
                why=why,
                period_id=period_id,
                run=run,
            )
        )
    return out


def _index_run(path: Path) -> tuple[str, int, str] | None:
    from dsl41.canon import ARTIFACT_FORMAT_VERSION, CanonError, decode

    try:
        payload = decode(path.read_bytes())
    except (OSError, CanonError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("artifact_format_version") != ARTIFACT_FORMAT_VERSION:
        return None  # unversioned or foreign-version evidence: unreadable, floored
    job, run_number = payload.get("job"), payload.get("run_number")
    run_id = payload.get("run_id")
    if (
        isinstance(job, str)
        and isinstance(run_number, int)
        and not isinstance(run_number, bool)
        and isinstance(run_id, str)
    ):
        return (job, run_number, run_id)
    return None


def _log_artifacts(
    scan: _Scan,
    run: tuple[str, int],
    verdict: Verdict,
    rule: str,
    why: str,
    period_id: int | None,
) -> list[Artifact]:
    """`logs/<job>.<run_number>.out` and `.err`, when they are where the
    default resolver puts them.

    A job that set `std_out_file` appends to a file of its own choosing,
    outside this root as often as not, and nothing here touches it. The
    logs take the run's verdict, so a spool and its output go together
    rather than leaving a directory of orphans.

    The names come from `runner_adapters.default_log_paths`, which is
    where the convention lives. This module never loads the catalog, so it
    cannot ask `job_log_paths`; what it can do is not spell the filename a
    second time."""
    from dsl41.runner_adapters import default_log_paths

    out: list[Artifact] = []
    for path in default_log_paths(run[0], run[1], scan.run_root):
        if path.exists():
            out.append(
                Artifact(
                    path=path,
                    kind="run_log",
                    verdict=verdict,
                    rule=rule,
                    why=why,
                    period_id=period_id,
                    run=run,
                )
            )
    return out


def _not_a_spool(entry: Path, run: tuple[str, int] | None) -> str | None:
    """Why this entry under `runs/` is not a spool this estate wrote, or
    None when it is one.

    Three ways to fail, and they are told apart because a report that
    says the wrong thing about WHY is a report an operator argues with.
    A SYMLINK is refused whatever it points at: the plan reads a name and
    the removal follows a link, so a spool that is a link is one where
    those two are not the same object -- and `rmtree` refuses it anyway,
    which would be an error at deletion time rather than a verdict."""
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
    """Remove the plan's prunable artifacts that the operator's flags
    select, and report everything else.

    Policy is the caller's: `classes`, `older_than_days` and `keep_runs`
    are how an operator states which of the licensed deletions to perform.
    Floors are not policy and cannot be reached from here.

    A removal that the OPERATING SYSTEM refuses -- a permission, a
    vanished directory, a name that is not what the plan read -- is
    recorded and the sweep continues. Deletion is irreversible and
    partial, so the report has to say which artifacts went and which did
    not; stopping at the first error would leave an operator with a
    traceback and no list. A removal the PLAN refuses is different and
    still raises: that is a floor being reached, which is a defect here
    rather than a fact about the disk."""
    unknown = sorted(set(classes) - set(CLASSES))
    if unknown:
        raise EngineError(
            f"unknown retention class(es) {', '.join(unknown)}:"
            f" this verb knows {', '.join(sorted(CLASSES))}"
        )
    selected = _selected(
        plan, classes=classes, older_than_days=older_than_days, keep_runs=keep_runs
    )
    chosen = {(str(item.path), item.kind) for item in selected}
    removed: list[Artifact] = []
    failed: list[tuple[Artifact, str]] = []
    wedged: set[tuple[str, int]] = set()
    bytes_removed = 0
    for item in selected:
        size = _size_of(item.path)
        if dry_run:
            bytes_removed += size
            continue
        if item.run is not None and item.run in wedged:
            # one artifact of this run already refused, so the rest of it
            # stays too: a directory kept while its index entry went would
            # leave the pair ss11a keeps one-to-one broken in the one
            # direction that authorizes a spawn
            failed.append((item, f"{item.run[0]}.{item.run[1]} could not be removed whole"))
            continue
        try:
            _remove(plan, item)
        except (OSError, ArtifactChanged) as exc:
            failed.append((item, str(exc)))
            if item.run is not None:
                wedged.add(item.run)
            continue
        bytes_removed += size
        removed.append(item)
    return PruneReport(
        removed=tuple(selected if dry_run else removed),
        kept=tuple(item for item in plan.prunable() if (str(item.path), item.kind) not in chosen),
        floored=plan.floors(),
        held=plan.held(),
        failed=tuple(failed),
        bytes_removed=bytes_removed,
        dry_run=dry_run,
    )


def _selected(
    plan: RetentionPlan,
    *,
    classes: Iterable[str],
    older_than_days: float | None,
    keep_runs: int,
) -> list[Artifact]:
    """The prunable artifacts the flags choose, thresholds applied.

    Both thresholds filter RUNS and then carry their index entries and
    logs with them: a run directory removed while its index entry stayed
    would leave the pair ss11a spends its whole protocol keeping
    one-to-one.

    `keep_runs` is per JOB, because `run_number` is per job (I2). Ranking
    every run of every job on one list by run number is comparing numbers
    from different series: a busy job's fifth run would outrank a quiet
    job's first, and one `--keep-runs 3` would delete the quiet job's
    whole history while keeping three of the busy one's.

    `older_than_days` reads the run's own artifacts, whichever survive. An
    orphaned index entry beside a hand-deleted directory is still that
    run's, and a threshold that only looked at the directory would sweep
    it while the same threshold protected the pair a moment earlier."""
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
    """The path's modification time, or `inf` when it cannot be read.

    `inf` and not 0: this feeds a DELETION filter, where an unreadable
    stat has to read as "too new to touch" rather than as "older than
    anything"."""
    try:
        return path.stat().st_mtime
    except OSError:
        return float("inf")


def _remove(plan: RetentionPlan, item: Artifact) -> None:
    """Delete one artifact, after proving the plan allows it (PR-36b,
    PR-36c).

    Three proofs, because one is not enough. The verdict says the SPEC
    licenses it; the run-root containment says this verb cannot reach the
    anchor, another estate or anything the operator merely pointed at; and
    `retained_over` says the path holds no floored or held artifact
    beneath it. Deleting a floored artifact is impossible here, rather than
    merely not done."""
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
        # `relative_to` is lexical: a crafted path could smuggle a `..`
        # that the descriptor walk would then follow OUT of the proven root
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
    # DESCRIPTOR-relative from here: the root fd is verified against the
    # plan's inode ONCE, and every step to the artifact walks openat with
    # O_NOFOLLOW from that fd -- a rename or retarget of any component
    # after the verification cannot redirect the deletion into another
    # estate, because no path is ever resolved again
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
            # ISOLATE first, atomically: whatever the name holds is moved
            # to a scratch name in the same directory, so no rename can
            # swap it between the identity proof and the deletion. The
            # scratch name is UNPREDICTABLE (uuid4) and checked free
            # first: Python exposes no portable RENAME_NOREPLACE, and a
            # deterministic name could be pre-seeded with a retained
            # artifact for the rename to destroy -- guessing a fresh uuid
            # inside the check-to-rename window is not a practical attack
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
                # not the artifact the plan verified. It is NOT renamed
                # back: without a no-replace rename primitive, any restore
                # can overwrite an entry that appeared at the name in the
                # meantime -- so the isolated entry stays under its scratch
                # name and the refusal reports exactly where it is
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
    """Delete `name` relative to an already-verified directory fd, never
    following a symlink and never resolving a path string.

    Every entry's identity is checked against the plan's RETAINED set
    before it is touched: a floored artifact moved inside a prunable
    directory after planning refuses rather than being swept away. The
    refusal leaves the partially-removed tree under its scratch name --
    the prunable content around it was licensed to go, and the retained
    artifact itself is untouched."""
    st = os.lstat(name, dir_fd=dir_fd)
    ident = (st.st_dev, st.st_ino)
    if ident in retained:
        raise ArtifactChanged(
            f"{shown}: a retained artifact was moved beneath this deletion target"
            " since the plan was computed -- refusing (period-model ss12, PR-36c)"
        )
    if ident not in licensed:
        # the inverse licence, PER ARTIFACT: the verdict covered what the
        # snapshot saw at and beneath THIS artifact, and anything else --
        # unobserved, or observed under a class the operator did not
        # select -- was never licensed for deletion here
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
    return Path(os.path.realpath(str(path)))


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


def _fsync_parent(path: Path) -> None:
    """A deletion is a directory-entry write, and a rename without an
    `fsync` of the parent is not durable across a power loss. The same
    rule the liturgy applies to every create applies to every unlink.

    A failure PROPAGATES: a suppressed fsync error would report a removal
    as done while a power cut can bring the entry back -- and for a
    tombstone pair that resurrection is the half that authorizes a
    spawn."""
    fsync_dir(path.parent)
