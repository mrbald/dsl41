"""Retention floors and the `estate prune` verb (period-model ss11a, ss12;
DL-135).

Obligations in ss13 exercised here: PR-36b and PR-36c.

House style follows test_boundary.py and test_estate.py: every floor
asserts the message fragment only its own rule produces, and every floor
has a passing counterpart beside it -- a build in which nothing is ever
prunable would satisfy "the floor refuses" on its own, and prove nothing.

The estates are built by the real machinery (genesis, the cutoff barrier,
`audit`), never by writing artifacts into a directory: what the floors are
computed FROM has to be what the engine actually leaves behind. The one
exception is the ss11a tombstone, which a detached supervisor writes and
these tests have none of -- so the fixture reconstructs it from the
binding the WAL already holds, and never mints a `run_id` of its own.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from typer.testing import CliRunner

from dsl41.attest import audit_period, verify_attestation
from dsl41.boundary import (
    ClaimedHead,
    read_seal,
    EstateAnchor,
    ClosedHead,
    OpenHead,
    default_anchor_dir,
    read_candidate,
)
from dsl41.canon import canonical_bytes
from dsl41.cli import app
from dsl41.period import (
    RuntimeProfile,
    archivable_names,
    archive_receipt_path,
    attestation_path,
    read_sentinel,
    read_archive_receipt,
    bundle_dir,
    period_dir,
    read_period_manifest,
    seal_path,
    stage_manifest,
    staging_dir,
    wal_path,
    write_bundle,
)
from dsl41.retention import CLASSES, Artifact, plan_retention, prune
from pydantic import ValidationError
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_journal import read_journal
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_procid import durable_write, mkdir_durable
from dsl41.runner_startup import start_run
from dsl41.oracle_state import Event

from test_boundary import (  # noqa: F401  (fixtures shared by design)
    C1_JIL,
    C2_JIL,
    T0,
    DETACHED,
    _catalog,
    _close,
    _crash_at,
    _no_crash,
    _refused,
    _request,
    _resume,
    _seal,
    _stage,
    _StubSupervisor,
)

#: a third catalog, so three periods reference three DIFFERENT content
#: addresses and a bundle's verdict is about reachability rather than
#: about two periods sharing one directory
C3_JIL = "insert_job: a\njob_type: c\ncommand: x\n\ninsert_job: b\njob_type: c\ncommand: THIRD\n"

runner = CliRunner()


# ------------------------------------------------------------- fixtures


def _open_period_one(
    run_root: Path,
    *,
    profile: RuntimeProfile | None = None,
    adapter: FakeAdapter | None = None,
):
    """A period-1 root whose CMD adapter completes for `b`, so a job that
    starts also ends -- `test_boundary._genesis` parks every run by design
    and a parked run can never be a terminal one. `a` still parks, which
    is what the carried-execution fixture needs."""
    catalog, sources = _catalog(C1_JIL)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=profile or RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    return start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": adapter or FakeAdapter(script={("b", 1): (0.0, 0)}, default=None)},
        staged=staged,
    )


def _run_job(engine, job: str) -> None:
    engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": job}))
    asyncio.run(engine.run_until_quiescent(T0))


def _spawn_effects(run_root: Path, period_id: int) -> list[dict[str, Any]]:
    """Every SPAWN effect one period's WAL recorded, in order. The BINDING
    the fixture below builds its spool from."""
    return [
        effect
        for record in read_journal(wal_path(run_root, period_id))
        if record.get("rec") == "decision"
        for effect in record.get("effects") or ()
        if effect.get("kind") == "SPAWN"
    ]


def _tombstone(run_root: Path, effect: dict[str, Any], *, terminal: bool = True) -> Path:
    """The ss11a tombstone a detached supervisor writes, reconstructed from
    an effect the WAL already holds: the run directory, the `.by_run_id`
    index entry, `spawn.json`, a default log, and `status.json` when the
    run ended.

    The `run_id` is READ off the effect. A fixture that minted its own
    would be proving the retention rule against a run this estate never
    planned, and the plan resolves a directory to a period through exactly
    that binding."""
    job, run_number, run_id = effect["job"], effect["run_number"], effect["run_id"]
    run_dir = run_root / "runs" / f"{job}.{run_number}"
    mkdir_durable(str(run_dir))
    durable_write(str(run_dir / "spawn.json"), canonical_bytes({"run_id": run_id}) + b"\n")
    if terminal:
        durable_write(
            str(run_dir / "status.json"),
            canonical_bytes({"run_id": run_id, "kind": "exited", "exit_code": 0}) + b"\n",
        )
    index = run_root / "runs" / ".by_run_id"
    mkdir_durable(str(index))
    durable_write(
        str(index / run_id),
        canonical_bytes(
            {
                "artifact_format_version": 1,
                "run_id": run_id,
                "job": job,
                "run_number": run_number,
            }
        )
        + b"\n",
    )
    logs = run_root / "logs"
    mkdir_durable(str(logs))
    durable_write(str(logs / f"{job}.{run_number}.out"), b"one line of output\n")
    return run_dir


def _anchor(run_root: Path) -> EstateAnchor:
    return EstateAnchor(default_anchor_dir(run_root))


def _attest(run_root: Path, period_id: int) -> None:
    audit_period(run_root, period_id, anchor=_anchor(run_root))


def _periods(
    run_root: Path, count: int, *, attest_through: int = 0, texts: list[str] | None = None
) -> None:
    """A root that has crossed `count - 1` boundaries with nothing live,
    attested through `attest_through`.

    By default every period runs a DIFFERENT catalog, so the manifests,
    the bundles and the sidecars are distinguishable by content. `texts`
    overrides that for the one case where sharing is the point: a period
    that reverts to earlier bytes references the bundle already there."""
    texts = list(texts) if texts is not None else [C2_JIL, C3_JIL]
    engine = _open_period_one(run_root)
    for boundary_no in range(1, count):
        text = texts[(boundary_no - 1) % len(texts)]
        staged = _stage(run_root, text)
        asyncio.run(_seal(engine, _request(engine, staged, request_id=f"r-seal-{boundary_no}")))
        _close(engine)
        engine = _resume(run_root, text)
    _close(engine)
    for period_id in range(1, attest_through + 1):
        _attest(run_root, period_id)


def _by_path(plan, path: Path, kind: str | None = None) -> Artifact:
    matches = [
        item for item in plan.artifacts if item.path == path and (kind is None or item.kind == kind)
    ]
    assert matches, f"{path} is in no verdict at all: retention must have an opinion on it"
    assert len(matches) == 1, matches
    return matches[0]


def _invoke(*args: str):
    return runner.invoke(app, list(args), catch_exceptions=False)


# ------------------------------------------- ss12 the head's reach (PR-36c)


def test_pr36c_the_sentinel_and_the_anchor_are_floored_and_unreachable(tmp_path: Path) -> None:
    """PR-36c: the two artifacts that say this directory belongs to a
    lineage are floored, and `prune` cannot be made to delete them.

    They have no "after the head moves past them" case, deliberately: a
    root without a sentinel is a root an old binary treats as unused, and
    an anchor is the lineage's only head."""
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    plan = plan_retention(run_root)
    sentinel = _by_path(plan, run_root / "journal.jsonl")
    anchor = _by_path(plan, default_anchor_dir(run_root) / "anchor.json")
    assert (sentinel.verdict, anchor.verdict) == ("floored", "floored")
    for item in (sentinel, anchor):
        with pytest.raises(EngineError, match="prunable artifacts and nothing else"):
            _force_remove(plan, item)
    # and the verb itself reaches neither, whatever it is asked for
    prune(plan, classes=["tombstones", "quarantine"], dry_run=False)
    assert (run_root / "journal.jsonl").exists()
    assert (default_anchor_dir(run_root) / "anchor.json").exists()


def _force_remove(plan, item: Artifact) -> None:
    """Ask the module to delete one artifact directly -- the only way a
    caller could try to reach a floor at all, since the verb iterates the
    prunable set."""
    from dsl41.retention import _remove

    _remove(plan, item)


def test_pr36c_the_active_claim_is_floored_and_a_consumed_one_is_not(tmp_path: Path) -> None:
    """PR-36c: a `claimed` head names a durable claim, and the claim is
    what a crashed claimant resumes through -- so it is floored while the
    head names it, and not once the head has moved on."""
    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    anchor = _anchor(run_root)
    head = anchor.read().head  # type: ignore[union-attr]
    assert isinstance(head, ClosedHead)
    anchor.acquire()
    claim = anchor.claim_successor(
        estate_id=anchor.read().estate_id,  # type: ignore[union-attr]
        seal_digest=head.seal_digest,
        next_period=2,
        target_root=run_root,
    )
    anchor.release()
    assert isinstance(_anchor(run_root).read().head, ClaimedHead)  # type: ignore[union-attr]
    claimed = plan_retention(run_root)
    entry = _by_path(claimed, anchor.claim_path(claim.claim_id))
    assert entry.verdict == "floored" and "resumable by claim_id" in entry.why

    _close(_resume(run_root, C2_JIL))  # the head moves claimed -> open
    assert isinstance(_anchor(run_root).read().head, OpenHead)  # type: ignore[union-attr]
    moved = plan_retention(run_root)
    assert anchor.claim_path(claim.claim_id) not in {item.path for item in moved.floors()}


def test_pr36c_the_opening_and_closing_sidecars_are_floored_and_an_older_one_is_not(
    tmp_path: Path,
) -> None:
    """PR-36c: the seal the current period opened from and the one it will
    close with are floored; a sidecar the head has moved past is released
    once a later checkpoint covers it.

    Recovery selects its seal by lineage and refuses without the sidecar,
    so a rule that could delete either was a rule that could delete the
    only artifact able to open the head."""
    two = tmp_path / "two"
    _periods(two, 2, attest_through=1)
    plan = plan_retention(two)
    opening = _by_path(plan, seal_path(two, 1))
    assert opening.verdict == "floored"
    assert opening.why == "the seal the current period opened from"

    closed = tmp_path / "closed"
    engine = _open_period_one(closed)
    asyncio.run(_seal(engine, _request(engine, _stage(closed, C2_JIL))))
    _close(engine)
    closing = _by_path(plan_retention(closed), seal_path(closed, 1))
    assert closing.verdict == "floored"
    assert closing.why == "the seal this period closed with"

    three = tmp_path / "three"
    _periods(three, 3, attest_through=2)
    later = plan_retention(three)
    assert _by_path(later, seal_path(three, 1)).verdict == "held"
    assert _by_path(later, seal_path(three, 2)).verdict == "floored"  # still the opening one


def test_pr36c_the_current_and_committed_next_manifests_are_floored(tmp_path: Path) -> None:
    """PR-36c: the current period's manifest and the one its seal committed
    the opening of are floored; a manifest the head has moved past is
    released."""
    closed = tmp_path / "closed"
    engine = _open_period_one(closed)
    asyncio.run(_seal(engine, _request(engine, _stage(closed, C2_JIL))))
    _close(engine)
    plan = plan_retention(closed)
    assert _by_path(plan, period_dir(closed, 1) / "manifest.json").verdict == "floored"
    committed = _by_path(plan, period_dir(closed, 2) / "manifest.json")
    assert committed.verdict == "floored"
    assert committed.why == "the committed-next period's pins"

    three = tmp_path / "three"
    _periods(three, 3, attest_through=2)
    later = plan_retention(three)
    assert _by_path(later, period_dir(three, 1) / "manifest.json").verdict == "held"
    assert _by_path(later, period_dir(three, 3) / "manifest.json").verdict == "floored"


def test_pr36c_an_uncommitted_candidate_keeps_its_two_files_until_the_seal_commits(
    tmp_path: Path,
) -> None:
    """PR-36c: `staged_manifest.json` and `candidate.json` beside an
    installed period manifest are floored until the seal that installed
    them commits.

    Recovery after an install-before-seal crash is decided by exactly those
    two files: the rename to `periods/N+1/` drops the digest from the path,
    so the installed candidate must carry its own identity (PR-30d)."""
    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    staged = _stage(run_root, C2_JIL)
    engine.crash_point = _crash_at("after_install")  # type: ignore[method-assign]
    asyncio.run(_refused(engine, _request(engine, staged)))
    assert read_candidate(period_dir(run_root, 2)) is not None
    uncommitted = plan_retention(run_root)
    for name in ("staged_manifest.json", "candidate.json"):
        entry = _by_path(uncommitted, period_dir(run_root, 2) / name)
        assert entry.verdict == "floored"
        assert "install-before-seal" in entry.why

    engine.crash_point = _no_crash  # type: ignore[method-assign]
    asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)
    committed = plan_retention(run_root)
    for name in ("staged_manifest.json", "candidate.json"):
        entry = _by_path(committed, period_dir(run_root, 2) / name)
        # committed, so recovery no longer reads them -- and DL-144 puts
        # them in the archive class UNDER THE SAME COVER as period 2's WAL,
        # which is the current period here, so the archive is blocked and
        # the verdict says which dependency blocks it
        assert entry.verdict == "held" and entry.rule == "DL-144"
        assert "the seal that installed it committed" in entry.why
        assert "this root retains no segment for this period" in entry.why


def test_pr36c_a_bundle_a_reachable_manifest_names_is_floored(tmp_path: Path) -> None:
    """PR-36c: bundles and their `sources.json` are floored BY REFERENCE.

    A bundle is content-addressed and shared -- a period that reverts to
    earlier bytes references the directory already there -- so the rule
    cannot be "the current period's number"."""
    run_root = tmp_path / "run"
    _periods(run_root, 3, attest_through=2)
    plan = plan_retention(run_root)
    current = read_period_manifest(run_root, 3)
    assert current is not None
    reachable = _by_path(plan, bundle_dir(run_root, current.source_bundle_hash))
    assert reachable.verdict == "floored"
    assert (reachable.path / "sources.json").exists()  # the file the floor is FOR
    older = read_period_manifest(run_root, 1)
    assert older is not None and older.source_bundle_hash != current.source_bundle_hash
    assert _by_path(plan, bundle_dir(run_root, older.source_bundle_hash)).verdict == "held"


def test_pr36c_a_bundle_two_periods_share_is_floored_by_the_reachable_one(
    tmp_path: Path,
) -> None:
    """The sharing case, where a by-period-number rule and a by-reference
    rule give different answers.

    Period 3 reverts to period 1's bytes, so ONE content-addressed
    directory is named by a manifest behind the head and by the current
    one. It is floored, and period 2's -- named by nothing reachable --
    is not."""
    run_root = tmp_path / "run"
    _periods(run_root, 3, attest_through=2, texts=[C2_JIL, C1_JIL])
    shared = read_period_manifest(run_root, 3)
    first = read_period_manifest(run_root, 1)
    middle = read_period_manifest(run_root, 2)
    assert shared is not None and first is not None and middle is not None
    assert shared.source_bundle_hash == first.source_bundle_hash  # one directory
    plan = plan_retention(run_root)
    assert _by_path(plan, bundle_dir(run_root, shared.source_bundle_hash)).verdict == "floored"
    assert _by_path(plan, bundle_dir(run_root, middle.source_bundle_hash)).verdict == "held"
    # and period 1's own manifest is still behind the head: the bundle's
    # verdict came from the REFERENCE, not from the period number
    assert _by_path(plan, period_dir(run_root, 1) / "manifest.json").verdict == "held"


def test_pr36c_the_latest_attestation_is_floored_and_a_superseded_one_is_not(
    tmp_path: Path,
) -> None:
    """PR-36c: the latest chain checkpoint and every attestation after it
    are floored; an earlier one is released because the later checkpoint
    covers it by induction."""
    run_root = tmp_path / "run"
    _periods(run_root, 3, attest_through=1)
    only_one = plan_retention(run_root)
    assert _by_path(only_one, attestation_path(run_root, 1)).verdict == "floored"

    _attest(run_root, 2)
    two = plan_retention(run_root)
    superseded = _by_path(two, attestation_path(run_root, 1))
    assert superseded.verdict == "held"
    assert superseded.why.startswith("checkpoint 2 covers it by induction")
    assert _by_path(two, attestation_path(run_root, 2)).verdict == "floored"


def test_a_checkpoint_that_does_not_verify_unlocks_nothing(tmp_path: Path) -> None:
    """A checkpoint is read as PROOF or not at all. A file that exists and
    does not verify counts as absent, which floors more rather than less --
    the safe direction, because the alternative authorizes a deletion on a
    chain that was never established."""
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    assert plan_retention(run_root).attested == frozenset({1})

    intact = attestation_path(run_root, 1).read_bytes()
    attestation_path(run_root, 1).write_bytes(intact.replace(b'"period_id":1', b'"period_id":9'))
    broken = plan_retention(run_root)
    assert broken.attested == frozenset()
    assert _by_path(broken, wal_path(run_root, 1)).verdict == "floored"
    attestation_path(run_root, 1).write_bytes(intact)  # the gate, not the machinery
    assert plan_retention(run_root).attested == frozenset({1})


def test_the_staging_directory_is_floored_and_a_quarantined_candidate_is_not(
    tmp_path: Path,
) -> None:
    """ss7/ss12: staged bytes a boundary is about to install are floored --
    the install refuses when they are gone, and names a retention sweep as
    the thing that could have removed them. A QUARANTINED candidate is the
    opposite case: being quarantined is what says no recovery reads it."""
    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    first = _stage(run_root, C2_JIL)
    engine.crash_point = _crash_at("after_install")  # type: ignore[method-assign]
    asyncio.run(_refused(engine, _request(engine, first)))
    second = _stage(run_root, C3_JIL)
    engine.crash_point = _no_crash  # type: ignore[method-assign]
    asyncio.run(_seal(engine, _request(engine, second)))
    _close(engine)
    # a THIRD candidate, staged and not yet asked for: exactly the state
    # the install refuses to meet an empty `.staging` in
    opened = _resume(run_root, C3_JIL)
    pending = _stage(run_root, C1_JIL)
    _close(opened)

    plan = plan_retention(run_root)
    quarantined = [item for item in plan.artifacts if item.kind == "quarantine"]
    assert len(quarantined) == 1 and quarantined[0].verdict == "prunable"
    staged_dirs = [item for item in plan.artifacts if item.kind == "staging"]
    assert [item.path for item in staged_dirs] == [staging_dir(run_root, pending.stage_digest)]
    assert staged_dirs[0].verdict == "floored"

    report = prune(plan, classes=["quarantine"], dry_run=False)
    assert [item.path for item in report.removed] == [quarantined[0].path]
    assert not quarantined[0].path.exists()
    assert staged_dirs[0].path.exists()


def test_pruning_refuses_every_floored_artifact_one_by_one(tmp_path: Path) -> None:
    """PR-36c's shape as one sweep: not a sample, every floor the plan
    holds. A rule added later without a refusal is caught here rather than
    by whoever meets it in an estate."""
    run_root = tmp_path / "run"
    _periods(run_root, 3, attest_through=2)
    plan = plan_retention(run_root)
    floors = plan.floors()
    assert {item.kind for item in floors} >= {"sentinel", "anchor", "sidecar", "manifest", "wal"}
    for item in floors:
        with pytest.raises(EngineError, match="prunable artifacts and nothing else"):
            _force_remove(plan, item)
        assert item.path.exists()


def test_prune_refuses_a_directory_that_holds_a_retained_artifact(tmp_path: Path) -> None:
    """The containment half of the guard: removing a directory removes what
    is inside it, so a plan item that CONTAINS a floor is refused too.

    Comparing paths for equality alone would let `runs/` go while every
    floored tombstone under it was named in this very plan."""
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    plan = plan_retention(run_root)
    forged = Artifact(
        path=run_root / "wal",
        kind="wal",
        verdict="prunable",
        rule="ss12",
        why="a rule that had not thought about containment",
    )
    with pytest.raises(EngineError, match="would take .* with it"):
        _force_remove(plan, forged)
    assert wal_path(run_root, 1).exists() and wal_path(run_root, 2).exists()


def test_prune_refuses_a_path_outside_the_run_root(tmp_path: Path) -> None:
    """The anchor lives outside every archivable root by design, and so
    this verb prunes one estate root and never its neighbour."""
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    plan = plan_retention(run_root)
    forged = Artifact(
        path=default_anchor_dir(run_root) / "anchor.json",
        kind="anchor",
        verdict="prunable",
        rule="ss1.3",
        why="a rule that had not thought about the anchor",
    )
    with pytest.raises(EngineError, match="retention prunes one estate"):
        _force_remove(plan, forged)
    assert (default_anchor_dir(run_root) / "anchor.json").exists()


# ---------------------------------- ss11a the idempotency store (PR-36b)


def _estate_with_runs(run_root: Path, *, carried: bool) -> dict[str, dict[str, Any]]:
    """A period-1 root that ran `b` to completion and, when `carried`,
    holds `a` live across the boundary. Returns the SPAWN effects by job."""
    engine = _open_period_one(run_root, profile=DETACHED if carried else None)
    _run_job(engine, "b")
    if carried:
        _run_job(engine, "a")
    effects = {effect["job"]: effect for effect in _spawn_effects(run_root, 1)}
    for job, effect in effects.items():
        _tombstone(run_root, effect, terminal=not (carried and job == "a"))
    if carried:
        engine.supervisor = _StubSupervisor(  # ss8: a detached seal proves its supervisor
            listing={
                "incarnation": "inc-1",
                "runs": [
                    {
                        "run_id": effects["a"]["run_id"],
                        "job": "a",
                        "run_number": effects["a"]["run_number"],
                        "wrapper_alive": True,
                    }
                ],
            }
        )  # type: ignore[assignment]
    staged = _stage(run_root, C2_JIL, profile=DETACHED if carried else None)
    asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)
    return effects


def _estate_with_many(run_root: Path, counts: dict[str, int]) -> dict[tuple[str, int], Any]:
    """A period-1 root that ran each named job the given number of times,
    every run terminal, every tombstone written, and then sealed.

    Two jobs at different run counts is what tells a per-job `--keep-runs`
    from one that ranks every run of every job on a single list."""
    engine = _open_period_one(run_root, adapter=FakeAdapter())
    for job, times in counts.items():
        for _ in range(times):
            _run_job(engine, job)
    effects = {(e["job"], e["run_number"]): e for e in _spawn_effects(run_root, 1)}
    for effect in effects.values():
        _tombstone(run_root, effect)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    return effects


def test_pr36b_a_replayable_spawn_tombstone_is_floored(tmp_path: Path) -> None:
    """PR-36b: the run directory and its `.by_run_id` entry may not go
    while the SPAWN effect that names them can still be replayed.

    "No index entry" means "first application", so deleting an index entry
    or a run directory AUTHORIZES a spawn -- which makes this a safety
    rule and not housekeeping."""
    run_root = tmp_path / "run"
    effects = _estate_with_runs(run_root, carried=False)
    run_id = effects["b"]["run_id"]
    unattested = plan_retention(run_root)
    for path in (run_root / "runs" / "b.1", run_root / "runs" / ".by_run_id" / run_id):
        entry = _by_path(unattested, path)
        assert entry.verdict == "floored"
        assert entry.why == "period 1 is unattested and its audit needs this spool"
    report = prune(unattested, classes=["tombstones"], dry_run=False)
    assert report.removed == ()
    assert (run_root / "runs" / "b.1" / "spawn.json").exists()


def test_pr36b_an_attested_periods_terminal_run_may_go(tmp_path: Path) -> None:
    """PR-36b's other half: after the period is attested and the run is
    terminal, the tombstone may go -- directory, index entry and default
    logs together, because ss11a spends its whole protocol keeping the
    `run_id` and the `(job, run_number)` one-to-one."""
    run_root = tmp_path / "run"
    effects = _estate_with_runs(run_root, carried=False)
    _attest(run_root, 1)
    plan = plan_retention(run_root)
    run_id = effects["b"]["run_id"]
    entry = _by_path(plan, run_root / "runs" / "b.1")
    assert entry.verdict == "prunable"
    assert entry.why == "period 1 is attested and the run is terminal"

    report = prune(plan, classes=["tombstones"], dry_run=False)
    gone = {item.path for item in report.removed}
    assert run_root / "runs" / "b.1" in gone
    assert run_root / "runs" / ".by_run_id" / run_id in gone
    assert run_root / "logs" / "b.1.out" in gone
    assert not (run_root / "runs" / "b.1").exists()
    assert not (run_root / "runs" / ".by_run_id" / run_id).exists()
    assert report.bytes_removed > 0
    # and the estate is still openable: the floors are untouched
    assert seal_path(run_root, 1).exists() and wal_path(run_root, 1).exists()
    assert plan_retention(run_root).prunable() == ()


def test_pr36b_a_carried_run_is_floored_while_the_period_it_ended_in_is_open(
    tmp_path: Path,
) -> None:
    """PR-36b's terminality half, read off the SEALS: a run named in seal
    N's `executions` was live at that boundary and ended in period N+1, so
    the floor holds until N+1 has closed and been attested.

    The counterpart is beside it in one plan: the run that ENDED inside
    period 1 is prunable under the same attestation."""
    run_root = tmp_path / "run"
    effects = _estate_with_runs(run_root, carried=True)
    _attest(run_root, 1)
    plan = plan_retention(run_root)
    carried = _by_path(plan, run_root / "runs" / "a.1")
    assert carried.verdict == "floored"
    assert carried.why == "live or carried into period 2, and this root holds no seal for it"
    assert _by_path(plan, run_root / "runs" / "b.1").verdict == "prunable"
    report = prune(plan, classes=["tombstones"], dry_run=False)
    assert {item.run for item in report.removed} == {("b", 1)}
    assert (run_root / "runs" / "a.1" / "spawn.json").exists()
    assert run_root / "runs" / ".by_run_id" / effects["a"]["run_id"] not in {
        item.path for item in report.removed
    }


def test_a_run_with_no_spawn_effect_in_the_retained_wal_is_floored(tmp_path: Path) -> None:
    """A directory whose provenance the retained WAL cannot show is
    floored. Nothing here guesses which period would have to be attested
    to release it, and a guess in this rule authorizes a spawn."""
    run_root = tmp_path / "run"
    _estate_with_runs(run_root, carried=False)
    _attest(run_root, 1)
    stranger = run_root / "runs" / "ghost.7"
    mkdir_durable(str(stranger))
    durable_write(str(stranger / "spawn.json"), b'{"run_id":"unknown"}\n')
    entry = _by_path(plan_retention(run_root), stranger)
    assert entry.verdict == "floored"
    assert "provenance unknown" in entry.why


def test_an_index_entry_this_binary_cannot_read_is_floored(tmp_path: Path) -> None:
    """An unreadable index entry still NAMES a `run_id`, and deleting it
    authorizes a spawn. It is floored rather than swept."""
    run_root = tmp_path / "run"
    _estate_with_runs(run_root, carried=False)
    _attest(run_root, 1)
    torn = run_root / "runs" / ".by_run_id" / "torn"
    durable_write(str(torn), b"{not json\n")
    entry = _by_path(plan_retention(run_root), torn)
    assert entry.verdict == "floored"
    assert "cannot read still names a run_id" in entry.why


def test_the_wal_of_an_unattested_period_is_floored_and_an_attested_one_is_held(
    tmp_path: Path,
) -> None:
    """ss12 floors the WAL of any unattested period; an attested one with
    no checkpoint ABOVE it is held, and the verdict names why (DL-144).

    Period 1 here is attested and period 2 is the current one, so no chain
    checkpoint covers period 1 yet: the archive class is real and this
    estate is still not eligible for it. A build that offered every
    attested WAL would pass a test that only asked "is it prunable
    eventually"."""
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    plan = plan_retention(run_root)
    attested = _by_path(plan, wal_path(run_root, 1))
    assert attested.verdict == "held" and attested.rule == "DL-144"
    assert "no chain checkpoint above period 1 covers it" in attested.why
    open_period = _by_path(plan, wal_path(run_root, 2))
    assert open_period.verdict == "floored" and open_period.rule == "ss12"
    assert plan.prunable() == ()  # nothing in a quiet two-period estate


# --------------------------------------------------------- the verb


def test_a_dry_run_deletes_nothing_and_reports_every_verdict(tmp_path: Path) -> None:
    """`--dry-run` lists what would go and what is retained, and touches
    nothing. It reports the plan's prunable set WHOLE, so a `--dry-run` and
    a run under the same flags cannot disagree about what was on the
    table."""
    run_root = tmp_path / "run"
    _estate_with_runs(run_root, carried=False)
    _attest(run_root, 1)
    plan = plan_retention(run_root)
    report = prune(plan, classes=CLASSES, dry_run=True)
    assert report.dry_run is True and report.failed == ()
    assert {item.path for item in report.removed} == {item.path for item in plan.prunable()}
    assert report.floored == plan.floors() and report.held == plan.held()
    # the size is REPORTED without being reclaimed: an operator deciding
    # whether to prune is asking how much it is worth
    assert report.bytes_removed > 0
    assert (run_root / "runs" / "b.1").exists()
    assert prune(plan, classes=CLASSES, dry_run=False).bytes_removed == report.bytes_removed


def test_prune_removes_only_the_classes_the_operator_named(tmp_path: Path) -> None:
    """Policy is the operator's: a class not named is reported as prunable
    and left alone."""
    run_root = tmp_path / "run"
    _estate_with_runs(run_root, carried=False)
    _attest(run_root, 1)
    plan = plan_retention(run_root)
    report = prune(plan, classes=["quarantine"], dry_run=False)
    assert report.removed == ()
    assert {item.kind for item in report.kept} == {"run", "run_index", "run_log"}
    assert (run_root / "runs" / "b.1").exists()
    with pytest.raises(EngineError, match="unknown retention class"):
        prune(plan, classes=["everything"], dry_run=False)


def test_keep_runs_and_an_age_threshold_keep_a_run_whole(tmp_path: Path) -> None:
    """The thresholds filter RUNS and carry the index entry and the logs
    with them: a directory removed while its index entry stayed would
    break the one-to-one ss11a spends its whole protocol keeping."""
    run_root = tmp_path / "run"
    _estate_with_runs(run_root, carried=False)
    _attest(run_root, 1)
    plan = plan_retention(run_root)
    assert prune(plan, classes=["tombstones"], dry_run=True, keep_runs=1).removed == ()
    fresh = prune(plan, classes=["tombstones"], dry_run=True, older_than_days=1.0)
    assert fresh.removed == ()  # written seconds ago
    aged = prune(plan, classes=["tombstones"], dry_run=True, older_than_days=0.0)
    assert {item.kind for item in aged.removed} == {"run", "run_index", "run_log"}


def test_keep_runs_is_per_job_and_never_starves_a_quiet_one(tmp_path: Path) -> None:
    """`run_number` is per JOB (I2), so ranking every run of every job on
    one list by run number compares numbers from different series.

    A busy job's fifth run would outrank a quiet job's first, and one
    `--keep-runs 1` would delete the quiet job's whole history while
    keeping the busy one's newest. Proved with two jobs at different run
    counts in one estate."""
    run_root = tmp_path / "run"
    effects = _estate_with_many(run_root, {"b": 3, "a": 1})
    _attest(run_root, 1)
    plan = plan_retention(run_root)
    kept_one = prune(plan, classes=["tombstones"], dry_run=True, keep_runs=1)
    removed = {item.run for item in kept_one.removed if item.run is not None}
    assert removed == {("b", 1), ("b", 2)}  # a.1 is job a's newest and stays
    assert ("a", 1) not in removed
    # and the whole estate is on the table when nothing is kept back
    every = prune(plan, classes=["tombstones"], dry_run=True)
    assert {item.run for item in every.removed if item.run is not None} == set(effects)


def test_an_age_threshold_covers_an_orphaned_index_entry(tmp_path: Path) -> None:
    """The age filter reads whichever of a run's artifacts survive.

    An index entry beside a hand-deleted directory is still that run's,
    and a threshold that only looked at the directory would sweep the
    entry a moment after protecting the pair."""
    run_root = tmp_path / "run"
    effects = _estate_with_runs(run_root, carried=False)
    _attest(run_root, 1)
    index = run_root / "runs" / ".by_run_id" / effects["b"]["run_id"]
    shutil.rmtree(run_root / "runs" / "b.1")
    plan = plan_retention(run_root)
    assert _by_path(plan, index).verdict == "prunable"
    protected = prune(plan, classes=["tombstones"], dry_run=True, older_than_days=1.0)
    assert index not in {item.path for item in protected.removed}
    assert index in {item.path for item in prune(plan, classes=["tombstones"]).removed}


def test_a_removal_the_filesystem_refuses_is_reported_and_the_run_stays_whole(
    tmp_path: Path,
) -> None:
    """Deletion is irreversible and partial, so a refusal by the FILESYSTEM
    is recorded and the sweep runs on -- and the rest of the refused run
    stays with it.

    A directory kept while its index entry went would break the pair
    ss11a keeps one-to-one, in the one direction that authorizes a spawn.
    The unremovable artifact is a stand-in: a path whose parent component
    is a regular FILE, so the descriptor-relative walk fails with ENOTDIR
    on every platform and every uid -- standing for the permission and
    the vanished directory an estate meets in the field. (A symlink is no
    longer a refusal: fd-relative removal unlinks the LINK, never follows
    it, which is the safe behaviour.)"""
    run_root = tmp_path / "run"
    effects = _estate_with_many(run_root, {"b": 2})
    _attest(run_root, 1)
    plan = plan_retention(run_root)
    blocker = run_root / "logs" / "zzz.1.out"
    blocker.write_text("a regular file, not a directory\n")
    unremovable = blocker / "x"
    forged = Artifact(
        path=unremovable,
        kind="run_log",
        verdict="prunable",
        rule="PR-36b",
        why="a stand-in for what the filesystem refuses",
        run=("b", 1),
        ident=(0, 0),  # the walk fails with ENOTDIR before identity is read
    )
    report = prune(
        # FIRST in the plan's order, so the failure lands before the rest
        # of run b.1 is reached -- which is the case the skip rule is for
        replace(plan, artifacts=(forged,) + plan.artifacts),
        classes=["tombstones"],
        dry_run=False,
    )

    assert unremovable in {item.path for item, _ in report.failed}
    # run b.1 stays WHOLE: neither the directory nor its index entry went
    assert (run_root / "runs" / "b.1").exists()
    assert (run_root / "runs" / ".by_run_id" / effects[("b", 1)]["run_id"]).exists()
    assert ("b", 1) not in {item.run for item in report.removed}
    # and the sweep continued: b.2 is gone, whole
    assert not (run_root / "runs" / "b.2").exists()
    assert not (run_root / "runs" / ".by_run_id" / effects[("b", 2)]["run_id"]).exists()


def test_a_symlinked_spool_is_floored_rather_than_deleted(tmp_path: Path) -> None:
    """The plan reads a NAME and the removal follows a link, so a spool
    that is a symlink is one where those two are not the same object. It
    is refused as a verdict, not as an error at deletion time."""
    run_root = tmp_path / "run"
    _estate_with_runs(run_root, carried=False)
    _attest(run_root, 1)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = run_root / "runs" / "c.9"
    link.symlink_to(elsewhere, target_is_directory=True)
    entry = _by_path(plan_retention(run_root), link)
    assert entry.verdict == "floored" and "symlink" in entry.why
    assert link.is_symlink() and elsewhere.exists()


def test_a_file_named_like_a_spool_says_why_it_is_not_one(tmp_path: Path) -> None:
    """A report that is wrong about WHY is a report an operator argues
    with: `b.9` parses as a name and is still not a directory."""
    run_root = tmp_path / "run"
    _estate_with_runs(run_root, carried=False)
    stray = run_root / "runs" / "b.9"
    durable_write(str(stray), b"not a spool\n")
    entry = _by_path(plan_retention(run_root), stray)
    assert entry.verdict == "floored"
    assert entry.why == "named like a spool and is not a directory: unowned, and left alone"


def test_the_verb_refuses_a_root_that_is_not_in_a_lineage(tmp_path: Path) -> None:
    """A root with no sentinel has no periods, so it has no floors to
    compute -- and D5's owner-local tombstone (DL-138) tells the operator
    WHICH kind of root they pointed at.

    `plan_retention` does not route through `read_journal`, so it carries
    its own registry: a retired `header` opening refuses BY NAME, while
    anything else at that path is unknown residue and refuses generically.
    Merging the two would lose the difference between an old root and a
    directory that was never an estate.

    HEADER-ONLY, and the `result`/`effect` cases are why the distinction is
    on the OPENING rather than on the record kind: both are retired records,
    and neither was ever a legal journal opening, so a file that starts with
    one is residue and not an old estate (DL-138, the L2 review)."""
    retired = tmp_path / "retired"
    retired.mkdir()
    (retired / "journal.jsonl").write_text(json.dumps({"rec": "header"}) + "\n")
    with pytest.raises(EngineError, match="RETIRED") as named:
        plan_retention(retired)
    assert "DL-138" in str(named.value)
    refused = _invoke("estate", "prune", "--run-root", str(retired), "--dry-run")
    assert refused.exit_code == 2 and "DL-138" in refused.output

    for kind in ("result", "effect"):
        never = tmp_path / f"never-{kind}"
        never.mkdir()
        (never / "journal.jsonl").write_text(json.dumps({"rec": kind}) + "\n")
        with pytest.raises(EngineError, match="holds no estate") as residue:
            plan_retention(never)
        assert "DL-138" not in str(residue.value) and "RETIRED" not in str(residue.value)

    garbage = tmp_path / "garbage"
    garbage.mkdir()
    (garbage / "journal.jsonl").write_text("not a record at all\n")
    with pytest.raises(EngineError, match="holds no estate") as generic:
        plan_retention(garbage)
    assert "DL-138" not in str(generic.value)


def test_the_verb_refuses_an_anchor_of_another_estate(tmp_path: Path) -> None:
    """One estate is never pruned by another's head. The registry is what
    says which periods are attested, and a stranger's registry says
    nothing about this root."""
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    other = tmp_path / "other"
    _periods(other, 1)
    with pytest.raises(EngineError, match="refusing to prune one estate"):
        plan_retention(run_root, anchor_dir=default_anchor_dir(other))


def test_the_cli_verb_lists_with_dry_run_and_refuses_without_a_class(tmp_path: Path) -> None:
    """`dsl41 estate prune` with no class named and no `--dry-run` deletes
    nothing and says so: a default set would be a retention policy, and
    that is the operator's."""
    run_root = tmp_path / "run"
    _estate_with_runs(run_root, carried=False)
    _attest(run_root, 1)
    listed = _invoke("estate", "prune", "--run-root", str(run_root), "--dry-run")
    assert listed.exit_code == 0
    assert "would remove" in listed.output and "floored (the model refuses)" in listed.output
    assert str(run_root / "runs" / "b.1") in listed.output
    assert (run_root / "runs" / "b.1").exists()

    nothing = _invoke("estate", "prune", "--run-root", str(run_root))
    assert nothing.exit_code == 2 and "name at least one class" in nothing.output
    assert (run_root / "runs" / "b.1").exists()

    removed = _invoke("estate", "prune", "--run-root", str(run_root), "--tombstones")
    assert removed.exit_code == 0 and "removed 3 artifact(s)" in removed.output
    assert not (run_root / "runs" / "b.1").exists()
    assert seal_path(run_root, 1).exists()


def test_the_report_names_the_rule_that_decided_each_verdict(tmp_path: Path) -> None:
    """A report tells an operator which sentence to argue with, not only
    what happened."""
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    rendered = _by_path(plan_retention(run_root), wal_path(run_root, 2)).render()
    assert "floored" in rendered and "ss12" in rendered and "period 2" in rendered


def test_a_rolled_roots_imported_seal_and_attestation_are_floored(tmp_path: Path) -> None:
    """PR-02f/PR-36c on a rolled root: it holds the seal it opened from and
    that seal's attestation, and NONE of the closing period's WAL.

    Those two artifacts are what a SECOND roll imports, so a retention
    walk over segments alone would have no opinion about either -- and an
    artifact with no verdict is one the guard cannot protect."""
    from dsl41.estate import roll_into_root

    root_a = tmp_path / "a"
    engine = _open_period_one(root_a)
    asyncio.run(_seal(engine, _request(engine, _stage(root_a, C2_JIL))))
    _close(engine)
    anchor_dir = default_anchor_dir(root_a)
    _attest(root_a, 1)
    catalog, _ = _catalog(C2_JIL)
    root_b = tmp_path / "b"
    roll_into_root(root_b, anchor_dir=anchor_dir, catalog_of=lambda _r, _m: catalog)

    plan = plan_retention(root_b, anchor_dir=anchor_dir)
    assert plan.attested == frozenset({1})  # verified from the IMPORTED pair
    assert _by_path(plan, seal_path(root_b, 1)).verdict == "floored"
    assert _by_path(plan, attestation_path(root_b, 1)).verdict == "floored"
    assert not wal_path(root_b, 1).exists()  # the roll imports no earlier WAL
    assert _by_path(plan, wal_path(root_b, 2)).verdict == "floored"
    for item in (seal_path(root_b, 1), attestation_path(root_b, 1)):
        with pytest.raises(EngineError, match="prunable artifacts and nothing else"):
            _force_remove(plan, _by_path(plan, item))
        assert item.exists()


# ------------------------------------------ peer-review round-1 pins (DL-135)


def test_the_planner_refuses_a_second_spawn_for_one_run(tmp_path: Path) -> None:
    """I2 at the planner: one (job, run_number) is one run, born once. A
    silently-kept FIRST binding would compute the tombstone floor for the
    wrong effect and prune a replayable one."""
    import copy
    import uuid as uuid_mod

    from dsl41.canon import canonical_bytes as canon

    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _run_job(engine, "b")
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    target = wal_path(run_root, 1)
    records = read_journal(target)
    donor_at = next(
        i for i, r in enumerate(records) if r.get("rec") == "decision" and (r.get("effects") or ())
    )
    forged = copy.deepcopy(records[donor_at])
    for eff in forged["effects"]:
        eff["run_id"] = str(uuid_mod.uuid4())  # a SECOND birth for the same run
        eff["effect_id"] = "e-forged"
    records.insert(donor_at + 1, forged)
    target.write_bytes(b"".join(canon(r) + b"\n" for r in records))
    with pytest.raises(EngineError, match="violates I2"):
        plan_retention(run_root)


def test_a_noncanonical_run_directory_never_takes_a_runs_verdict(tmp_path: Path) -> None:
    """`b.01` aliases `b.1`: a non-canonical spelling must not shadow a
    protected run's evidence under the shadowed run's verdict."""
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    alias = run_root / "runs" / "b.01"
    mkdir_durable(str(alias))
    (alias / "spawn.json").write_text("{}")
    plan = plan_retention(run_root)
    item = _by_path(plan, alias)
    assert item.verdict == "floored"  # unknown, never b.1's prunable
    assert item.run is None


def test_an_index_body_rewritten_to_a_terminal_run_refuses_the_plan(tmp_path: Path) -> None:
    """ss11a's pair is one-to-one BOTH ways: an index body rewritten to
    name a terminal run would take that run's prunable verdict while the
    filename still binds a live run's identity."""
    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _run_job(engine, "b")  # terminal (the adapter completes b)
    _run_job(engine, "a")  # parked: live
    run_a = next(e for e in _spawn_effects(run_root, 1) if e["job"] == "a")
    run_b = next(e for e in _spawn_effects(run_root, 1) if e["job"] == "b")
    _tombstone(run_root, run_b, terminal=True)
    _tombstone(run_root, run_a, terminal=False)
    _close(engine)
    index = run_root / "runs" / ".by_run_id" / run_a["run_id"]
    index.write_bytes(
        canonical_bytes(
            {
                "artifact_format_version": 1,
                "run_id": run_a["run_id"],
                "job": run_b["job"],
                "run_number": run_b["run_number"],
            }
        )
        + b"\n"
    )
    with pytest.raises(EngineError, match="pair is broken"):
        plan_retention(run_root)


def test_a_swapped_sidecar_and_attestation_pair_refuses_the_plan(tmp_path: Path) -> None:
    """ss12: a valid pair from ANOTHER estate verifies internally and
    names none of this estate's live runs -- deletion-authorizing seals
    are bound to the sentinel, the filename, the WAL record and the
    successor link."""
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    other = tmp_path / "other"
    _periods(other, 2, attest_through=1)
    shutil.copyfile(seal_path(other, 1), seal_path(run_root, 1))
    shutil.copyfile(attestation_path(other, 1), attestation_path(run_root, 1))
    with pytest.raises(EngineError, match="stranger's sidecar|successor opened from"):
        plan_retention(run_root)


def test_backfill_refuses_a_foreign_or_discontinuous_segment_chain(tmp_path: Path) -> None:
    """ss11/I1: a valid sealed segment from another estate renamed into
    place, or a missing middle segment, must refuse -- not stream a
    spliced or holed lineage without a gap marker."""
    from dsl41.runner_journal import read_backfill

    run_root = tmp_path / "run"
    _periods(run_root, 3)
    other = tmp_path / "other"
    _periods(other, 3)
    # foreign splice: another estate's segment 2 under this root's name
    saved = wal_path(run_root, 2).read_bytes()
    shutil.copyfile(wal_path(other, 2), wal_path(run_root, 2))
    # the sentinel binding fires first -- even a SINGLE replaced segment
    # that the early stop would read alone is caught by it
    with pytest.raises(EngineError, match="stranger's segment under this estate's name"):
        read_backfill(run_root / "journal.jsonl", since=0)
    wal_path(run_root, 2).write_bytes(saved)
    read_backfill(run_root / "journal.jsonl", since=0)  # restored: clean
    # missing middle
    wal_path(run_root, 2).unlink()
    with pytest.raises(EngineError, match="not contiguous"):
        read_backfill(run_root / "journal.jsonl", since=0)


def test_a_failed_parent_fsync_marks_the_removal_failed(tmp_path: Path, monkeypatch) -> None:
    """PR-36b: a suppressed fsync error would report a removal done while
    a power cut can bring the entry back -- and for a tombstone pair the
    resurrection is the half that authorizes a spawn."""
    import os as os_mod

    from dsl41.retention import prune

    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _run_job(engine, "b")
    effect = next(e for e in _spawn_effects(run_root, 1) if e["job"] == "b")
    _tombstone(run_root, effect, terminal=True)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    _attest(run_root, 1)
    plan = plan_retention(run_root)
    real_fsync = os_mod.fsync

    def broken(fd: int) -> None:
        raise OSError("fsync lost the disk")

    monkeypatch.setattr("os.fsync", broken)
    report = prune(plan, classes=("tombstones",), dry_run=False)
    monkeypatch.undo()
    assert report.removed == () or not report.removed  # nothing reported as done
    assert report.failed  # every attempted removal is on the failed list
    monkeypatch.setattr("os.fsync", real_fsync)


# ------------------------------------------ peer-review round-2 pins (DL-135)


def test_an_index_body_disagreeing_with_its_own_filename_refuses(tmp_path: Path) -> None:
    """ss11a: the body's run_id must BE the filename -- a rewritten body
    keeping the tuple would take the tuple's verdict while the entry's
    identity evidence says something else."""
    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _run_job(engine, "b")
    effect = next(e for e in _spawn_effects(run_root, 1) if e["job"] == "b")
    _tombstone(run_root, effect, terminal=True)
    _close(engine)
    entry = run_root / "runs" / ".by_run_id" / effect["run_id"]
    import uuid as uuid_mod

    entry.write_bytes(
        canonical_bytes(
            {
                "artifact_format_version": 1,
                "run_id": str(uuid_mod.uuid4()),  # not the filename
                "job": effect["job"],
                "run_number": effect["run_number"],
            }
        )
        + b"\n"
    )
    with pytest.raises(EngineError, match="disagrees with itself"):
        plan_retention(run_root)


def test_backfill_refuses_a_single_replaced_segment(tmp_path: Path) -> None:
    """ss1.2: the early stop can read ONE segment, where no adjacency
    check runs -- the sentinel is what binds even a lone replaced file to
    this estate."""
    from dsl41.runner_journal import read_backfill

    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _close(engine)
    other = tmp_path / "other"
    engine = _open_period_one(other)
    _close(engine)
    shutil.copyfile(wal_path(other, 1), wal_path(run_root, 1))
    with pytest.raises(EngineError, match="stranger's segment under this estate's name"):
        read_backfill(run_root / "journal.jsonl", since=0)


def test_backfill_refuses_a_traversed_seal_record_off_schema(tmp_path: Path) -> None:
    """ss2.2 at the stream: a `closes_at_index` rewritten to a bool would
    skip the exact-int continuity comparison instead of refusing."""
    from dsl41.canon import canonical_bytes as canon
    from dsl41.runner_journal import read_backfill

    run_root = tmp_path / "run"
    _periods(run_root, 2)
    target = wal_path(run_root, 1)
    records = read_journal(target)
    assert records[-1].get("rec") == "seal"
    records[-1] = {**records[-1], "closes_at_index": True}
    target.write_bytes(b"".join(canon(r) + b"\n" for r in records))
    with pytest.raises(EngineError, match="closes_at_index"):
        read_backfill(run_root / "journal.jsonl", since=0)


def test_backfill_refuses_a_segment_whose_opening_names_a_different_number(tmp_path: Path) -> None:
    """ss2.1: a foreign segment renamed into place parses and even seals;
    its own opening is what says which segment it actually is -- a filename
    and an opening that disagree is a stranger's file under this estate's
    name, even though its estate_id still matches the sentinel and so
    passes that earlier, separate check.

    `segment_no`, `period_id` and `opens_from_seal.period_id` must all agree
    WITH EACH OTHER too (I1, checked inside `read_journal` on the single
    file) -- so all three move together here, to the same wrong lineage,
    leaving that single-file check satisfied and isolating the cross-file
    check `read_backfill` alone owns: the number in the FILENAME."""
    from dsl41.canon import canonical_bytes as canon
    from dsl41.runner_journal import read_backfill

    run_root = tmp_path / "run"
    _periods(run_root, 2)
    target = wal_path(run_root, 2)
    records = read_journal(target)
    assert records[0].get("rec") == "segment"
    link = {**records[0]["opens_from_seal"], "period_id": 98}
    records[0] = {**records[0], "segment_no": 99, "period_id": 99, "opens_from_seal": link}
    target.write_bytes(b"".join(canon(r) + b"\n" for r in records))
    with pytest.raises(EngineError, match="a stranger's file under this estate's name"):
        read_backfill(run_root / "journal.jsonl", since=0)


def test_backfill_refuses_a_forged_opens_from_seal_digest(tmp_path: Path) -> None:
    """ss2.1/ss11: a segment's `opens_from_seal.digest` must be the seal it
    actually chains from, not merely a well-shaped one -- a forged digest
    that still passes the segment's own schema is a spliced or foreign
    lineage the chain proof must catch."""
    from dsl41.canon import canonical_bytes as canon
    from dsl41.runner_journal import read_backfill

    run_root = tmp_path / "run"
    _periods(run_root, 2)
    target = wal_path(run_root, 2)
    records = read_journal(target)
    assert records[0].get("rec") == "segment"
    link = dict(records[0]["opens_from_seal"])
    link["digest"] = "sha256:" + "0" * 64
    records[0] = {**records[0], "opens_from_seal": link}
    target.write_bytes(b"".join(canon(r) + b"\n" for r in records))
    with pytest.raises(EngineError, match="does not open from the seal"):
        read_backfill(run_root / "journal.jsonl", since=0)


def test_backfill_refuses_a_seal_and_its_next_segment_naming_different_estates(
    tmp_path: Path,
) -> None:
    """ss1.2 at the chain, not just at the sentinel: a closing seal whose
    OWN estate_id disagrees with the segment it closed into is two estates
    concatenated into one stream, even though each segment's own opening
    still matches the sentinel that bound it (the earlier, separate
    check)."""
    from dsl41.canon import canonical_bytes as canon
    from dsl41.runner_journal import read_backfill

    run_root = tmp_path / "run"
    _periods(run_root, 2)
    target = wal_path(run_root, 1)
    records = read_journal(target)
    assert records[-1].get("rec") == "seal"
    records[-1] = {**records[-1], "estate_id": "00000000-0000-4000-8000-000000000000"}
    target.write_bytes(b"".join(canon(r) + b"\n" for r in records))
    with pytest.raises(EngineError, match="two estates in one stream"):
        read_backfill(run_root / "journal.jsonl", since=0)


def test_backfill_refuses_a_seal_whose_closes_at_index_skips_the_next_segments_first(
    tmp_path: Path,
) -> None:
    """I2: continuity is by the EXACT index, not by adjacency alone -- a
    forged `closes_at_index` that still passes the seal's own schema, and
    still names the right next period and digest, must be caught against
    the next segment's own `first_index`."""
    from dsl41.canon import canonical_bytes as canon
    from dsl41.runner_journal import read_backfill

    run_root = tmp_path / "run"
    _periods(run_root, 2)
    target = wal_path(run_root, 1)
    records = read_journal(target)
    assert records[-1].get("rec") == "seal"
    records[-1] = {**records[-1], "closes_at_index": records[-1]["closes_at_index"] + 5}
    target.write_bytes(b"".join(canon(r) + b"\n" for r in records))
    with pytest.raises(EngineError, match="index frontier is not continuous"):
        read_backfill(run_root / "journal.jsonl", since=0)


def test_a_sidecar_whose_local_wal_lost_its_seal_record_refuses(tmp_path: Path) -> None:
    """ss2.2/ss12: a sidecar with a LOCAL WAL that lacks its naming record
    is not a committed boundary this lineage wrote -- and an unbound
    sidecar must not authorize a deletion."""
    from dsl41.canon import canonical_bytes as canon

    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    target = wal_path(run_root, 1)
    records = [r for r in read_journal(target) if r.get("rec") != "seal"]
    target.write_bytes(b"".join(canon(r) + b"\n" for r in records))
    with pytest.raises(EngineError, match="exactly the record that names"):
        plan_retention(run_root)


# ------------------------------------------ peer-review round-3 pins (DL-135)


def _attested_tombstone_root(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """One attested period with one terminal, tombstoned run: the smallest
    root that has anything prunable at all."""
    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _run_job(engine, "b")
    effect = next(e for e in _spawn_effects(run_root, 1) if e["job"] == "b")
    _tombstone(run_root, effect, terminal=True)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    _attest(run_root, 1)
    return run_root, effect


def test_a_retargeted_root_refuses_the_prune(tmp_path: Path) -> None:
    """ss12: a run-root symlink retargeted between the plan and the prune
    satisfies every path-string containment check inside the REPLACEMENT
    estate -- the plan is bound to the resolved root's inode."""
    import os as os_mod

    from dsl41.retention import prune

    run_root, _ = _attested_tombstone_root(tmp_path)
    other, _ = _attested_tombstone_root(tmp_path / "elsewhere")
    link = tmp_path / "the-root"
    os_mod.symlink(run_root, link)
    plan = plan_retention(link, anchor_dir=default_anchor_dir(run_root))
    os_mod.unlink(link)
    os_mod.symlink(other, link)  # retargeted under the plan
    with pytest.raises(EngineError, match="re-plan before pruning"):
        prune(plan, classes=("tombstones",), dry_run=False)
    assert (other / "runs" / ".by_run_id").is_dir()  # the stranger lost nothing


def test_a_spawn_without_a_run_id_floors_its_tombstone(tmp_path: Path) -> None:
    """ss11a: a null-run_id SPAWN with a tombstone on disk -- deleting the
    tombstone would sever the only identity evidence there is."""
    from dsl41.canon import canonical_bytes as canon

    run_root, effect = _attested_tombstone_root(tmp_path)
    target = wal_path(run_root, 1)
    records = read_journal(target)
    for record in records:
        if record.get("rec") != "decision":
            continue
        for eff in record.get("effects") or ():
            if eff.get("kind") == "SPAWN" and eff.get("job") == "b":
                eff["run_id"] = None
    target.write_bytes(b"".join(canon(r) + b"\n" for r in records))
    plan = plan_retention(run_root)
    run_dir = run_root / "runs" / "b.1"
    item = _by_path(plan, run_dir)
    assert item.verdict == "floored"
    assert "provenance incomplete" in item.why


def test_an_unversioned_index_entry_is_floored_not_pruned(tmp_path: Path) -> None:
    """ss11a: an index entry without its artifact_format_version is
    evidence this binary cannot read -- the supervisor refuses it, and
    retention must not delete what its own floor protects."""
    run_root, effect = _attested_tombstone_root(tmp_path)
    entry = run_root / "runs" / ".by_run_id" / effect["run_id"]
    entry.write_bytes(
        canonical_bytes(
            {"run_id": effect["run_id"], "job": "b", "run_number": 1}  # no version
        )
        + b"\n"
    )
    plan = plan_retention(run_root)
    item = _by_path(plan, entry)
    assert item.verdict == "floored"


def test_a_seal_record_with_an_unknown_field_refuses_the_plan(tmp_path: Path) -> None:
    """ss2.2: the full schema runs BEFORE the mirror comparison -- an
    unknown field on the point-of-no-return record is a record this
    binary does not fully understand, and it authorizes nothing."""
    from dsl41.canon import canonical_bytes as canon

    run_root, _ = _attested_tombstone_root(tmp_path)
    target = wal_path(run_root, 1)
    records = read_journal(target)
    assert records[-1].get("rec") == "seal"
    records[-1] = {**records[-1], "surprise": True}
    target.write_bytes(b"".join(canon(r) + b"\n" for r in records))
    with pytest.raises(EngineError, match="unknown"):
        plan_retention(run_root)


def test_backfill_refuses_a_header_wal_under_a_sentinel(tmp_path: Path) -> None:
    """ss1.1/ss11: a sentinel says this root is periodized, and a `header`
    WAL under its segment name is a file from before the period model.

    Since DL-138 that file refuses BY NAME at the record validator, before
    any chain proof runs -- the tombstone replaces the short-circuit the
    backfill used to carry, and the subscriber is told what is on the disk
    rather than being handed an unaffiliated stream."""
    from dsl41.runner_journal import read_backfill

    run_root = tmp_path / "run"
    _close(_open_period_one(run_root))
    wal_path(run_root, 1).write_text(
        json.dumps(
            {
                "rec": "header",
                "baseline_id": "b",
                "catalog_hash": "0" * 64,
                "state_machine_version": 1,
                "clock_domain": "virtual",
                "started_at": "2026-07-01T08:00:00",
            },
            sort_keys=True,
        )
        + "\n"
    )
    with pytest.raises(EngineError, match="RETIRED") as caught:
        read_backfill(run_root / "journal.jsonl", since=0)
    assert "DL-138" in str(caught.value) and "`header`" in str(caught.value)


# ------------------------------------------ peer-review round-4 pins (DL-135)


def test_a_boolean_run_number_refuses_the_plan(tmp_path: Path) -> None:
    """ss11a: `true` passes an isinstance(int) check and aliases run 1 --
    malformed identity evidence authorizes nothing."""
    from dsl41.canon import canonical_bytes as canon

    run_root, _ = _attested_tombstone_root(tmp_path)
    target = wal_path(run_root, 1)
    records = read_journal(target)
    for record in records:
        if record.get("rec") != "decision":
            continue
        for eff in record.get("effects") or ():
            if eff.get("kind") == "SPAWN" and eff.get("job") == "b":
                eff["run_number"] = True
    target.write_bytes(b"".join(canon(r) + b"\n" for r in records))
    with pytest.raises(EngineError, match="malformed identity evidence"):
        plan_retention(run_root)


def test_a_swapped_in_directory_at_the_same_pathname_refuses_the_prune(tmp_path: Path) -> None:
    """ss12: rename the planned root aside and place another estate at the
    SAME pathname -- the removal opens the root fresh, fstats it against
    the plan's inode, and refuses; no path is ever resolved again after
    that proof (descriptor-relative walk)."""
    import os as os_mod

    from dsl41.retention import prune

    run_root, _ = _attested_tombstone_root(tmp_path)
    victim, _ = _attested_tombstone_root(tmp_path / "elsewhere")
    plan = plan_retention(run_root)
    aside = tmp_path / "aside"
    os_mod.rename(run_root, aside)
    os_mod.rename(victim, run_root)  # another estate now answers to the plan's pathname
    try:
        with pytest.raises(EngineError, match="re-plan before pruning"):
            prune(plan, classes=("tombstones",), dry_run=False)
        assert (run_root / "runs" / ".by_run_id").is_dir()  # the swapped-in estate lost nothing
    finally:
        os_mod.rename(run_root, tmp_path / "elsewhere" / "run")
        os_mod.rename(aside, run_root)


# ------------------------------------------ peer-review round-5 pins (DL-135)


def test_a_dotdot_path_never_escapes_the_proven_root(tmp_path: Path) -> None:
    """ss1.1: `relative_to` is lexical, and a crafted `..` component would
    make the descriptor walk step OUT of the proven root."""
    run_root, _ = _attested_tombstone_root(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("another estate's bytes\n")
    plan = plan_retention(run_root)
    forged = Artifact(
        path=run_root / ".." / "victim.txt",
        kind="run_log",
        verdict="prunable",
        rule="PR-36b",
        why="a crafted escape",
        ident=(1, 1),
    )
    with pytest.raises(EngineError, match="traverses outside"):
        _force_remove(plan, forged)
    assert victim.exists()


def test_a_replaced_artifact_under_a_prunable_name_is_refused(tmp_path: Path) -> None:
    """ss12: artifact identity is pinned at PLAN time and verified through
    the held descriptor -- ANOTHER directory renamed onto a prunable name
    after planning must refuse, not be deleted under the prunable
    verdict."""
    import os as os_mod

    from dsl41.retention import prune

    run_root, effect = _attested_tombstone_root(tmp_path)
    plan = plan_retention(run_root)
    decoy = run_root / "runs" / "decoy"
    mkdir_durable(str(decoy))
    (decoy / "somebody-elses-evidence").write_text("kept\n")
    os_mod.rename(run_root / "runs" / "b.1", run_root / "runs" / "gone")
    os_mod.rename(decoy, run_root / "runs" / "b.1")  # the swap, after planning
    report = prune(plan, classes=("tombstones",), dry_run=False)
    assert any("not the artifact the plan was computed over" in why for _, why in report.failed)
    # NOT restored (a restore could clobber a newcomer): isolated under its
    # reported scratch name, contents intact
    assert any("the entry was left at" in why for _, why in report.failed)
    found = list((run_root / "runs").glob(".pruning-*/somebody-elses-evidence"))
    assert found and found[0].read_text() == "kept\n"


# ------------------------------------------ peer-review round-6 pins (DL-135)


def test_a_retained_artifact_moved_inside_a_prunable_tree_survives(tmp_path: Path) -> None:
    """PR-36c: a retained artifact moved INSIDE a prunable directory after
    planning must refuse mid-walk, not be swept away with the tree -- its
    identity is pinned, not its path."""
    import os as os_mod

    from dsl41.retention import prune

    run_root, effect = _attested_tombstone_root(tmp_path)
    plan = plan_retention(run_root)
    target = run_root / "runs" / f"{effect['job']}.{effect['run_number']}"
    assert _by_path(plan, target).verdict == "prunable"
    held_wal = wal_path(run_root, 1)
    assert _by_path(plan, held_wal).verdict in ("held", "floored")
    os_mod.rename(held_wal, target / "smuggled.jsonl")  # the move, after planning
    report = prune(plan, classes=("tombstones",), dry_run=False)
    assert any("moved beneath this deletion target" in why for _, why in report.failed)
    # the retained artifact itself is untouched (under the scratch name's tree)
    found = list(run_root.rglob("smuggled.jsonl"))
    assert found and found[0].read_bytes()  # bytes intact


# ------------------------------------------ peer-review round-7 pins (DL-135)


def test_the_scratch_name_is_unpredictable_and_never_renamed_over(tmp_path: Path) -> None:
    """ss12: the old deterministic scratch name could be pre-seeded with a
    retained artifact for the isolation rename to destroy. The name is now
    uuid-fresh per removal and checked free first; pre-seeding the OLD
    deterministic name is harmless."""
    import os as os_mod

    from dsl41.retention import prune

    run_root, effect = _attested_tombstone_root(tmp_path)
    plan = plan_retention(run_root)
    name = f"{effect['job']}.{effect['run_number']}"
    # the OLD deterministic scratch spelling, pre-seeded with bytes that
    # must survive
    decoy = run_root / "runs" / f".{name}.{os_mod.getpid()}.pruning"
    decoy.write_text("retained bytes at the old deterministic name\n")
    report = prune(plan, classes=("tombstones",), dry_run=False)
    assert decoy.exists() and decoy.read_text().startswith("retained bytes")
    assert any(item.path.name == name for item in report.removed)  # the prune itself worked


def test_a_mismatch_rollback_never_clobbers_a_new_entry(tmp_path: Path) -> None:
    """ss12: if something appears at the original name while the
    mismatched replacement is isolated, the rollback must not rename over
    it -- the isolated entry stays, and its location is reported."""
    import os as os_mod

    from dsl41.retention import ArtifactChanged, _remove

    run_root, effect = _attested_tombstone_root(tmp_path)
    plan = plan_retention(run_root)
    name = f"{effect['job']}.{effect['run_number']}"
    target = run_root / "runs" / name
    item = _by_path(plan, target)
    # replace the artifact AFTER planning: identity mismatch at removal
    os_mod.rename(target, run_root / "runs" / "aside")
    replacement = run_root / "runs" / name
    mkdir_durable(str(replacement))
    (replacement / "impostor").write_text("x\n")

    real_rename = os_mod.rename
    state = {"raced": False}

    def racing_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        # the FIRST rename is the isolation; the instant it completes,
        # something new lands at the original name
        real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if not state["raced"] and str(dst).startswith(".pruning-"):
            state["raced"] = True
            newcomer = run_root / "runs" / name
            newcomer.mkdir()
            (newcomer / "newborn").write_text("must survive\n")

    import dsl41.retention as retention_mod

    os_module = retention_mod.os
    original = os_module.rename
    os_module.rename = racing_rename  # type: ignore[assignment]
    try:
        with pytest.raises(ArtifactChanged, match="the entry was left at"):
            _remove(plan, item)
    finally:
        os_module.rename = original  # type: ignore[assignment]
    assert (run_root / "runs" / name / "newborn").read_text() == "must survive\n"
    leftovers = [p for p in (run_root / "runs").iterdir() if p.name.startswith(".pruning-")]
    assert leftovers and (leftovers[0] / "impostor").exists()  # isolated, reported, intact


# ------------------------------------------ peer-review round-8 pins (DL-135)


def test_a_file_moved_out_of_a_retained_tree_survives_the_sweep(tmp_path: Path) -> None:
    """ss12: a retained DIRECTORY's protection extends to each inode
    inside it -- a single file moved from a retained bundle into a
    prunable tree must refuse mid-walk, not be swept."""
    import os as os_mod

    from dsl41.retention import prune

    run_root, effect = _attested_tombstone_root(tmp_path)
    plan = plan_retention(run_root)
    target = run_root / "runs" / f"{effect['job']}.{effect['run_number']}"
    bundles = sorted((run_root / "catalogs").iterdir())
    bundle = next(b for b in bundles if b.is_dir() and (b / "sources.json").exists())
    os_mod.rename(bundle / "sources.json", target / "sources.json")  # after planning
    report = prune(plan, classes=("tombstones",), dry_run=False)
    assert any("moved beneath this deletion target" in why for _, why in report.failed)
    found = list(run_root.rglob("sources.json"))
    assert found  # the bundle file's inode survived, wherever it now sits


# ------------------------------------------ peer-review round-9 pin (DL-135)


def test_an_unreadable_retained_tree_refuses_the_plan(tmp_path: Path) -> None:
    """ss12: an unreadable subdirectory inside a retained tree leaves its
    children unpinned -- traversal fails CLOSED and the plan refuses,
    never silently under-protects."""
    import os as os_mod

    run_root, _ = _attested_tombstone_root(tmp_path)
    bundle = next(
        b
        for b in sorted((run_root / "catalogs").iterdir())
        if b.is_dir() and (b / "sources.json").exists()
    )
    sealed = bundle / "sealed-off"
    sealed.mkdir()
    (sealed / "inner.txt").write_text("unpinnable without access\n")
    os_mod.chmod(sealed, 0o000)
    try:
        with pytest.raises(EngineError, match="cannot fully (pin|observe)"):
            plan_retention(run_root)
    finally:
        os_mod.chmod(sealed, 0o700)


# ------------------------------------------ peer-review round-10 pin (DL-135)


def test_a_directory_swapped_between_listing_and_descent_refuses(
    tmp_path: Path, monkeypatch
) -> None:
    """ss12: file identities come from the directory enumeration itself
    (d_ino at read time -- no later lookup to race), and descending into a
    subdirectory re-proves it: the child is opened under the parent's fd
    and its fstat must equal what the listing reported."""
    import os as os_mod

    run_root, _ = _attested_tombstone_root(tmp_path)
    bundle = next(
        b
        for b in sorted((run_root / "catalogs").iterdir())
        if b.is_dir() and (b / "sources.json").exists()
    )
    real_open = os_mod.open
    state = {"raced": False}

    def racing_open(path, flags, *args, **kwargs):
        if (
            not state["raced"]
            and kwargs.get("dir_fd") is not None
            and isinstance(path, str)
            and path == bundle.name
        ):
            state["raced"] = True
            os_mod.rename(bundle, bundle.parent / "swapped-away")
            (bundle).mkdir()  # a NEW directory under the listed name
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("os.open", racing_open)
    try:
        with pytest.raises(EngineError, match="identity changed between listing and descent"):
            plan_retention(run_root)
    finally:
        monkeypatch.undo()


def test_an_identity_swap_during_the_scan_refuses_the_plan(tmp_path: Path, monkeypatch) -> None:
    """ss12: identity is captured when the estate is FIRST observed, and
    the bracket at the end re-proves every retained path still holds the
    observed inode -- a byte-identical substitution during the scan (the
    original drifting away unprotected) refuses the plan."""
    import os as os_mod
    import shutil as shutil_mod

    run_root, _ = _attested_tombstone_root(tmp_path)
    from dsl41 import retention as retention_mod

    real_sentinel = retention_mod.read_sentinel
    state = {"swapped": False}

    def swapping_sentinel(root):
        # runs AFTER the snapshot and before the verdict scan: replace the
        # WAL with a byte-identical copy (new inode), moving the original
        if not state["swapped"]:
            state["swapped"] = True
            wal = wal_path(run_root, 1)
            os_mod.rename(wal, run_root / "drifted")
            shutil_mod.copyfile(run_root / "drifted", wal)
        return real_sentinel(root)

    monkeypatch.setattr(retention_mod, "read_sentinel", swapping_sentinel)
    try:
        with pytest.raises(EngineError, match="changed identity while the plan"):
            plan_retention(run_root)
    finally:
        monkeypatch.undo()


# ----------------------------------------- peer-review round-14 pin (DL-135)


def test_a_prunable_swapped_during_the_scan_is_never_stamped(tmp_path: Path, monkeypatch) -> None:
    """ss12: prunable identity comes from the observation snapshot, and
    only while the disk still agrees -- a foreign directory swapped in
    during the scan gets ident None, and removal refuses it."""
    import os as os_mod
    import shutil as shutil_mod

    from dsl41.retention import prune

    run_root, effect = _attested_tombstone_root(tmp_path)
    target = run_root / "runs" / f"{effect['job']}.{effect['run_number']}"
    from dsl41 import retention as retention_mod

    real_sentinel = retention_mod.read_sentinel
    state = {"swapped": False}

    def swapping_sentinel(root):
        if not state["swapped"]:
            state["swapped"] = True
            os_mod.rename(target, run_root / "runs" / "original-away")
            foreign = tmp_path / "foreign"
            foreign.mkdir()
            (foreign / "not-ours").write_text("must survive\n")
            shutil_mod.move(str(foreign), str(target))
        return real_sentinel(root)

    monkeypatch.setattr(retention_mod, "read_sentinel", swapping_sentinel)
    try:
        plan = plan_retention(run_root)
    finally:
        monkeypatch.undo()
    report = prune(plan, classes=("tombstones",), dry_run=False)
    assert any("no identity for this artifact" in why for _, why in report.failed)
    assert (target / "not-ours").read_text() == "must survive\n"  # the foreign dir survived


# ----------------------------------------- peer-review round-15 pin (DL-135)


def test_an_unobserved_artifact_inside_a_prunable_tree_survives(tmp_path: Path) -> None:
    """ss12, the inverse licence: the plan's verdict covered what the
    snapshot SAW -- an inode that was nowhere in the estate at observation
    and moved into a prunable tree after planning was never licensed and
    survives."""
    import shutil as shutil_mod

    from dsl41.retention import prune

    run_root, effect = _attested_tombstone_root(tmp_path)
    plan = plan_retention(run_root)
    target = run_root / "runs" / f"{effect['job']}.{effect['run_number']}"
    foreign = tmp_path / "unobserved.txt"
    foreign.write_text("never part of this estate\n")
    shutil_mod.move(str(foreign), str(target / "unobserved.txt"))  # after planning
    report = prune(plan, classes=("tombstones",), dry_run=False)
    assert any("did not license beneath this deletion" in why for _, why in report.failed)
    found = list(run_root.rglob("unobserved.txt"))
    assert found and found[0].read_text() == "never part of this estate\n"


# ----------------------------------------- peer-review round-16 pin (DL-135)


def test_an_unselected_class_moved_into_a_selected_tree_survives(tmp_path: Path) -> None:
    """ss12: the deletion licence is PER ARTIFACT -- an observed inode
    from a class the operator did not select, moved inside a selected
    tree after planning, refuses instead of riding along."""
    import shutil as shutil_mod

    from dsl41.retention import prune

    run_root, effect = _attested_tombstone_root(tmp_path)
    quarantine = run_root / "periods" / ".quarantine" / "superseded"
    mkdir_durable(str(quarantine))
    (quarantine / "candidate.json").write_text("{}\n")
    plan = plan_retention(run_root)
    target = run_root / "runs" / f"{effect['job']}.{effect['run_number']}"
    # observed AND prunable -- but under --quarantine, which is NOT selected
    shutil_mod.move(str(quarantine), str(target / "smuggled-quarantine"))
    report = prune(plan, classes=("tombstones",), dry_run=False)
    assert any("did not license beneath this deletion" in why for _, why in report.failed)
    found = list(run_root.rglob("smuggled-quarantine/candidate.json"))
    assert found  # the unselected class survived


# --------------------------------------------------- arch-review pin (DL-137)


def test_the_index_dir_constants_agree_across_the_tier_boundary() -> None:
    """DL-42 forbids the supervisor tier importing dsl41, so `.by_run_id`
    is spelled twice by licence -- and nothing asserted the two spellings
    were equal until now."""
    from dsl41.retention import INDEX_DIR
    from dsl41.runner_supervisor import _INDEX_DIR

    assert INDEX_DIR == _INDEX_DIR


# ------------------------------------------------- coverage-gate pins


def test_a_reopened_periods_missing_manifest_refuses_resume(tmp_path: Path) -> None:
    """ss7 phase 3 (runner_startup._reopened): a period that opened from a
    seal is re-seeded from that seal at EVERY resume, not only the one that
    first opened it -- so its committed manifest must still be there the
    SECOND time too. Missing it then is a period whose artifacts were
    pruned under a live lineage, which retention's own floor forbids."""
    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    _close(_resume(run_root, C2_JIL))  # opens period 2 for the first time
    (period_dir(run_root, 2) / "manifest.json").unlink()
    with pytest.raises(EngineError, match="re-seeded from that seal at every resume"):
        _resume(run_root, C2_JIL)  # re-resuming an ALREADY open period


# ================================================== the archive (DL-144)
#
# PR-53..PR-56. PR-Q3/E20 is closed by policy: a seal-only archive may
# stand in for pruned inputs, under conditions this block holds the code
# to one at a time. Every estate here is built by the real machinery and
# every id is read off it -- the receipt binds a seal digest and an
# attestation digest, and a fixture that minted either would prove only
# that the string comparison runs.


def _archivable(run_root: Path, periods: int = 4) -> None:
    """A root whose period 1 (and 2) are archivable: several boundaries,
    attested through the second-newest closed period, so a LATER chain
    checkpoint covers the ones below it."""
    _periods(run_root, periods, attest_through=periods - 1)


def _archive(run_root: Path, *, dry_run: bool = False):
    from dsl41.period import ARCHIVE_CLASS

    return prune(plan_retention(run_root), classes=(ARCHIVE_CLASS,), dry_run=dry_run)


def test_pr53_the_receipt_is_durable_before_the_first_deletion(tmp_path: Path) -> None:
    """PR-53: the point of no return is an artifact, and it is written
    FIRST.

    The receipt names the estate, the period, the seal, the attestation it
    stands on and the exact licensed list -- and everything in that list is
    then, and only then, deleted. A build that deleted first and receipted
    after would pass every "the archive works" test and lose an estate on
    its first crash."""
    from dsl41.canon import canonical_bytes as canon_bytes
    from dsl41.period import ARCHIVE_CLASS, archive_receipt_path, read_archive_receipt

    run_root = tmp_path / "run"
    _archivable(run_root)
    seen: list[tuple[str, bool]] = []
    import dsl41.retention as retention_mod

    real_remove = retention_mod._remove

    def watching(plan, item):
        # at EVERY deletion the receipt for that period must already be on
        # disk -- that is the ordering the whole class rests on
        seen.append((str(item.path), archive_receipt_path(run_root, item.period_id).exists()))
        real_remove(plan, item)

    retention_mod._remove = watching  # type: ignore[assignment]
    try:
        report = _archive(run_root)
    finally:
        retention_mod._remove = real_remove  # type: ignore[assignment]

    assert seen and all(present for _, present in seen), seen
    assert report.refused == () and report.failed == ()
    receipt = read_archive_receipt(run_root, 1)
    assert receipt is not None
    assert receipt.period_id == 1 and receipt.retention_class == ARCHIVE_CLASS
    assert receipt.estate_id == plan_retention(run_root).estate_id
    assert receipt.seal_digest == read_seal(run_root, 1).digest
    assert receipt.archived == ("wal/000001.jsonl",)
    # ss3.2: one artifact, one byte form, and the digest is over the file
    raw = archive_receipt_path(run_root, 1).read_bytes()
    assert raw == receipt.to_bytes()
    # the stamped digest is over the canonical bytes with only the
    # TOP-LEVEL `digest` removed, exactly as every other ss3.2 artifact
    body = {key: value for key, value in receipt.model_dump(mode="json").items()}
    assert canon_bytes({**body, "digest": receipt.digest}) == raw
    assert receipt.digest not in canon_bytes(body).decode()
    assert not wal_path(run_root, 1).exists()


def test_pr53_a_crash_between_the_receipt_and_the_deletions_completes(tmp_path: Path) -> None:
    """PR-53: a retry re-reads the receipt and finishes the sweep.

    The re-plan must offer exactly the artifacts the receipt still names,
    saying so, and it must NOT write a second receipt over the first: the
    stored list is what a retry completes from."""
    from dsl41.period import archive_receipt_path, read_archive_receipt

    run_root = tmp_path / "run"
    _archivable(run_root)
    import dsl41.retention as retention_mod

    real_remove = retention_mod._remove

    def crashing(plan, item):
        raise OSError("power loss between the receipt and the deletions")

    retention_mod._remove = crashing  # type: ignore[assignment]
    try:
        first = _archive(run_root)
    finally:
        retention_mod._remove = real_remove  # type: ignore[assignment]
    assert first.removed == () and first.failed
    stamped = archive_receipt_path(run_root, 1).read_bytes()
    assert wal_path(run_root, 1).exists()  # nothing went

    resumed = plan_retention(run_root)
    wal = _by_path(resumed, wal_path(run_root, 1))
    assert wal.verdict == "prunable" and wal.rule == "DL-144"
    assert wal.why == "the receipt is durable; completing the archive"
    report = _archive(run_root)
    assert not wal_path(run_root, 1).exists()
    # the receipt is the SAME artifact, byte for byte: a rewrite would
    # re-stamp `archived_at` and replace the list a retry completes from
    assert archive_receipt_path(run_root, 1).read_bytes() == stamped
    assert report.refused == ()
    assert read_archive_receipt(run_root, 1) is not None


def test_pr53_the_point_of_no_return_is_behind_a_receipted_period(tmp_path: Path) -> None:
    """PR-53: once the receipt is durable, eligibility stops being asked.

    Crash between the receipt and the deletions, then put a tombstone of
    that period BACK on disk -- the very dependency that had to be pruned
    before the archive was allowed. The sweep must still complete: the
    receipt has already published the weaker claim, and a plan that
    re-litigated the conditions would leave an estate that can neither
    finish the archive nor undo it."""
    from dsl41.period import ARCHIVE_CLASS, read_archive_receipt

    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _run_job(engine, "b")
    effect = _spawn_effects(run_root, 1)[0]
    for boundary_no in range(1, 4):
        text = [C2_JIL, C3_JIL][(boundary_no - 1) % 2]
        asyncio.run(
            _seal(engine, _request(engine, _stage(run_root, text), request_id=f"r-{boundary_no}"))
        )
        _close(engine)
        engine = _resume(run_root, text)
    _close(engine)
    for period_id in (1, 2, 3):
        _attest(run_root, period_id)

    import dsl41.retention as retention_mod

    real_remove = retention_mod._remove
    retention_mod._remove = lambda plan, item: (_ for _ in ()).throw(OSError("power loss"))
    try:
        prune(plan_retention(run_root), classes=(ARCHIVE_CLASS,), dry_run=False)
    finally:
        retention_mod._remove = real_remove  # type: ignore[assignment]
    assert read_archive_receipt(run_root, 1) is not None
    assert wal_path(run_root, 1).exists()

    _tombstone(run_root, effect)  # the pruned spool, back on disk
    resumed = plan_retention(run_root)
    # and the receipted period does not block the ones ABOVE it either:
    # the prefix is satisfied by the point of no return, not by the file
    assert _by_path(resumed, wal_path(run_root, 2)).verdict == "prunable"
    finished = prune(resumed, classes=(ARCHIVE_CLASS,), dry_run=False)
    assert finished.refused == () and finished.failed == ()
    assert not wal_path(run_root, 1).exists()
    assert not wal_path(run_root, 2).exists()


def test_pr53_two_sweeps_racing_one_receipt_do_not_write_two_lists(tmp_path: Path) -> None:
    """PR-53: the plan is read without a lock, so two sweeps can both see
    no receipt and both reach the write.

    The loser must act on the WINNER's list rather than on its own: the
    stored list is what a retry completes from, and a loser whose own
    selection the stored receipt does not license refuses instead of
    deleting under a licence nobody issued.

    The race is staged rather than threaded -- a plan computed before the
    receipt landed IS the loser's plan, exactly -- so what runs here is the
    stored-receipt path and not `durable_create`'s `FileExistsError`, which
    only a same-instant write reaches and which the caller reports as a
    refusal."""
    from dsl41.period import ARCHIVE_CLASS, archive_receipt_path

    run_root = tmp_path / "run"
    _archivable(run_root)
    losers = [plan_retention(run_root), plan_retention(run_root)]  # before any receipt
    assert all(plan.archived == frozenset() for plan in losers)
    _archive(run_root)  # the winner writes both receipts and deletes
    assert not wal_path(run_root, 1).exists()

    # the loser now runs its stale plan: the receipts are there, its own
    # artifacts are gone, and it neither rewrites a receipt nor refuses
    stamped = archive_receipt_path(run_root, 1).read_bytes()
    report = prune(losers[0], classes=(ARCHIVE_CLASS,), dry_run=False)
    assert archive_receipt_path(run_root, 1).read_bytes() == stamped
    assert report.refused == ()

    # and a loser whose selection the STORED receipt does not license
    # refuses rather than deleting under a licence nobody issued. The
    # winner here took the OTHER legal shape -- the segment alone, without
    # period 2's committed candidate pair -- so the loser's two candidate
    # items are outside the licence that was actually issued
    receipt = read_archive_receipt(run_root, 2)
    assert receipt is not None
    assert set(receipt.archived) == set(archivable_names(2))
    segment_only = receipt.model_copy(update={"archived": ("wal/000002.jsonl",)})
    durable_write(str(archive_receipt_path(run_root, 2)), segment_only.to_bytes())
    second = prune(losers[1], classes=(ARCHIVE_CLASS,), dry_run=False)
    refused = {str(item.path): why for item, why in second.refused}
    for name in ("candidate.json", "staged_manifest.json"):
        path = str(period_dir(run_root, 2) / name)
        assert path in refused, refused
        assert "does not name this artifact" in refused[path]
    assert archive_receipt_path(run_root, 2).read_bytes() == segment_only.to_bytes()


def test_pr53_an_archived_period_never_blocks_the_ones_above_it(tmp_path: Path) -> None:
    """PR-53: eligibility stops being asked of a receipted period, and the
    PREFIX above it stays open.

    The state under test is a sweep that got period 1's receipt down, could
    not delete its segment, and never reached period 2's receipt. Put
    period 1's pruned spool back and every itemized condition it once met
    now fails -- so a plan that re-litigated it would mark it blocked, and
    the prefix rule would then hold period 2 for a period whose archive is
    already committed. That is a floor nothing could ever lift, on an
    artifact the estate has already published the weaker claim about."""
    from dsl41.period import ARCHIVE_CLASS, read_archive_receipt

    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _run_job(engine, "b")
    effect = _spawn_effects(run_root, 1)[0]
    for boundary_no in range(1, 4):
        text = [C2_JIL, C3_JIL][(boundary_no - 1) % 2]
        asyncio.run(
            _seal(engine, _request(engine, _stage(run_root, text), request_id=f"r-{boundary_no}"))
        )
        _close(engine)
        engine = _resume(run_root, text)
    _close(engine)
    for period_id in (1, 2, 3):
        _attest(run_root, period_id)
    _tombstone(run_root, effect)
    prune(plan_retention(run_root), classes=("tombstones",), dry_run=False)

    import dsl41.retention as retention_mod

    real_write = retention_mod.write_archive_receipt

    def only_the_first(run_root_, receipt):
        if receipt.period_id > 1:
            raise OSError("power loss between the two receipts")
        real_write(run_root_, receipt)

    real_remove = retention_mod._remove

    def refusing_the_segment(plan_, item):
        # the segment survives too, so period 1 is archived AND still has
        # the very file its eligibility would be re-argued over
        if item.kind == "wal":
            raise OSError("the filesystem refused period 1's segment")
        real_remove(plan_, item)

    retention_mod.write_archive_receipt = only_the_first  # type: ignore[assignment]
    retention_mod._remove = refusing_the_segment  # type: ignore[assignment]
    try:
        prune(plan_retention(run_root), classes=(ARCHIVE_CLASS,), dry_run=False)
    finally:
        retention_mod.write_archive_receipt = real_write  # type: ignore[assignment]
        retention_mod._remove = real_remove  # type: ignore[assignment]
    assert read_archive_receipt(run_root, 1) is not None
    assert read_archive_receipt(run_root, 2) is None
    assert wal_path(run_root, 1).exists()

    _tombstone(run_root, effect)  # period 1's spool, back on disk
    resumed = plan_retention(run_root)
    assert _by_path(resumed, wal_path(run_root, 1)).verdict == "prunable"
    assert _by_path(resumed, wal_path(run_root, 2)).verdict == "prunable"
    finished = prune(resumed, classes=(ARCHIVE_CLASS,), dry_run=False)
    assert finished.refused == () and finished.failed == ()
    assert not wal_path(run_root, 1).exists()
    assert not wal_path(run_root, 2).exists()


def test_pr53_a_crash_before_the_receipt_leaves_nothing_behind(tmp_path: Path) -> None:
    """PR-53: the other side of the point of no return.

    Fail while the receipt is being written and the estate is exactly what
    it was: every input present, no receipt, and the period still
    derivation-verified."""
    from dsl41.attest import DERIVATION_VERIFIED, verified_tier
    from dsl41.period import archive_receipt_path, read_archive_receipt

    run_root = tmp_path / "run"
    _archivable(run_root)
    import dsl41.period as period_mod

    real_write = period_mod.write_archive_receipt

    def failing(run_root_, receipt):
        raise OSError("power loss inside the receipt write")

    import dsl41.retention as retention_mod

    retention_mod.write_archive_receipt = failing  # type: ignore[assignment]
    try:
        report = _archive(run_root)
    finally:
        retention_mod.write_archive_receipt = real_write  # type: ignore[assignment]

    assert report.removed == ()
    assert {item.period_id for item, _ in report.refused} == {1, 2}
    assert not archive_receipt_path(run_root, 1).exists()
    assert read_archive_receipt(run_root, 1) is None
    assert wal_path(run_root, 1).exists()
    assert verified_tier(run_root, 1) == DERIVATION_VERIFIED


def test_pr54_the_archive_refuses_until_the_spool_is_pruned(tmp_path: Path) -> None:
    """PR-54, and PR-36b's ORDER: a period's WAL may not be archived while
    a run born in it still has a tombstone on disk.

    The tombstone floor resolves a run directory to a period through the
    SPAWN effect in that period's WAL. Archive the WAL first and every
    tombstone it explains becomes provenance-unknown and floored FOREVER --
    a floor nothing can lift. The refusal names what remains, and the same
    estate archives cleanly the moment the spool goes."""
    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _run_job(engine, "b")
    effect = _spawn_effects(run_root, 1)[0]
    run_dir = _tombstone(run_root, effect)
    for boundary_no in range(1, 4):
        text = [C2_JIL, C3_JIL][(boundary_no - 1) % 2]
        asyncio.run(_seal(engine, _request(engine, _stage(run_root, text), request_id=f"r-{boundary_no}")))
        _close(engine)
        engine = _resume(run_root, text)
    _close(engine)
    for period_id in (1, 2, 3):
        _attest(run_root, period_id)

    blocked = _by_path(plan_retention(run_root), wal_path(run_root, 1))
    assert blocked.verdict == "held" and blocked.rule == "DL-144"
    assert "its spool is still on disk and must be pruned first" in blocked.why
    assert str(run_dir) in blocked.why
    assert _archive(run_root).removed == ()
    assert wal_path(run_root, 1).exists()

    # each of the THREE tombstone artifacts blocks on its own, because
    # each on its own is a run this WAL is the only explanation of: the
    # directory, the `.by_run_id` entry, and the default log
    index = run_root / "runs" / ".by_run_id" / effect["run_id"]
    log = run_root / "logs" / f"{effect['job']}.{effect['run_number']}.out"
    assert index.exists() and log.exists()
    aside = tmp_path / "aside"
    for position, alone in enumerate((run_dir, index, log)):
        # moved OUT of the estate, never renamed inside it: a `.aside`
        # suffix on an index entry breaks ss11a's filename-is-the-run_id
        # rule, and the planner would refuse for that reason instead
        holding = aside / str(position)
        holding.mkdir(parents=True)
        others = [path for path in (run_dir, index, log) if path != alone]
        for path in others:
            shutil.move(str(path), str(holding / path.name))
        still = _by_path(plan_retention(run_root), wal_path(run_root, 1))
        assert still.verdict == "held", alone
        assert str(alone) in still.why, alone
        for path in others:
            shutil.move(str(holding / path.name), str(path))

    # the SAME estate, one step later: prune the tombstones, then archive
    prune(plan_retention(run_root), classes=("tombstones",), dry_run=False)
    assert not run_dir.exists() and not index.exists() and not log.exists()
    freed = _by_path(plan_retention(run_root), wal_path(run_root, 1))
    assert freed.verdict == "prunable"
    assert _archive(run_root).removed
    assert not wal_path(run_root, 1).exists()


def test_pr54_a_cover_questioned_between_the_plan_and_the_receipt_refuses(
    tmp_path: Path,
) -> None:
    """PR-54: eligibility is RE-CHECKED against the live disk immediately
    before the receipt, independently of the plan.

    A re-check that re-read the plan would prove only that the plan had not
    changed, which it cannot. Here the covering checkpoint is corrupted
    after planning: the archive refuses, names the period, writes no
    receipt and deletes nothing."""
    from dsl41.period import archive_receipt_path, attestation_path as attest_path

    run_root = tmp_path / "run"
    _archivable(run_root)
    plan = plan_retention(run_root)
    assert [item.period_id for item in plan.prunable() if item.kind == "wal"] == [1, 2]

    # the checkpoint that COVERS period 1 stops proving anything, and so
    # does every one above it: the estate is left with no cover at all
    for period_id in (2, 3):
        attest_path(run_root, period_id).write_bytes(b'{"artifact_format_version":1}\n')
    from dsl41.period import ARCHIVE_CLASS

    report = prune(plan, classes=(ARCHIVE_CLASS,), dry_run=False)
    assert report.removed == ()
    assert report.refused
    assert all(
        "no chain checkpoint above period" in why or "no longer verifies" in why
        for _, why in report.refused
    ), report.refused
    assert not archive_receipt_path(run_root, 1).exists()
    assert wal_path(run_root, 1).exists()


def test_pr54_eligibility_is_itemized_and_every_block_names_its_dependency(
    tmp_path: Path,
) -> None:
    """PR-54: "everything the head has moved past" was the rule this class
    was NOT given.

    One estate, four verdicts, each naming the dependency in the way --
    unattested, the estate head, no later checkpoint, and an unarchived
    older period. A build that offered every attested WAL would pass a test
    that only asked whether the archive ever works."""
    run_root = tmp_path / "run"
    _archivable(run_root, periods=4)
    plan = plan_retention(run_root)
    assert _by_path(plan, wal_path(run_root, 4)).why == "the WAL of an unattested period"
    third = _by_path(plan, wal_path(run_root, 3))
    assert third.verdict == "held"
    assert "no chain checkpoint above period 3 covers it" in third.why
    assert [item.period_id for item in plan.prunable() if item.kind == "wal"] == [1, 2]

    # the PREFIX rule: hold period 1 back and period 2 is held with it,
    # because the archive runs oldest-first
    (run_root / "seals" / "000001.audit.json").unlink()
    held_back = plan_retention(run_root)
    first = _by_path(held_back, wal_path(run_root, 1))
    second = _by_path(held_back, wal_path(run_root, 2))
    assert first.verdict == "floored" and first.why == "the WAL of an unattested period"
    assert second.verdict == "held"
    assert "an older period this root retains is not archivable" in second.why


def test_pr55_the_receipt_the_attestation_and_the_sidecar_are_unreachable(
    tmp_path: Path,
) -> None:
    """PR-55: three artifacts per archived period, floored PERMANENTLY.

    Delete the receipt and the archive reads as loss; delete either of the
    others and the period has neither inputs nor proof. `_remove` refuses
    each one rather than merely not being asked."""
    from dsl41.period import archive_receipt_path

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    plan = plan_retention(run_root)
    for path, why in (
        (archive_receipt_path(run_root, 1), "reads as loss"),
        (attestation_path(run_root, 1), "PERMANENT floor"),
        (seal_path(run_root, 1), "PERMANENT floor"),
    ):
        item = _by_path(plan, path)
        assert item.verdict == "floored" and item.rule == "DL-144", item
        assert why in item.why
        with pytest.raises(EngineError, match="removes prunable artifacts and nothing else"):
            _force_remove(plan, item)


def test_pr55_restoring_the_inputs_does_not_undo_the_archive(tmp_path: Path) -> None:
    """PR-55: the archive is IRREVERSIBLE and the RECEIPT governs.

    Files put back beside a receipt do not move a period back to
    derivation-verified: the weaker claim is already published, and a tier
    that flickered with the contents of a directory would be no tier at
    all. The restored inputs may still be READ, which is why the plan
    offers the WAL for the deletion the receipt already licensed rather
    than refusing the estate."""
    from dsl41.attest import ATTESTATION_VERIFIED, verified_tier

    run_root = tmp_path / "run"
    _archivable(run_root)
    keep = wal_path(run_root, 1).read_bytes()
    _archive(run_root)
    assert verified_tier(run_root, 1) == ATTESTATION_VERIFIED

    wal_path(run_root, 1).write_bytes(keep)  # restored beside the receipt
    assert verified_tier(run_root, 1) == ATTESTATION_VERIFIED
    audited = _invoke("audit", "--run-root", str(run_root), "--period", "1")
    assert audited.exit_code == 0
    assert "attestation-verified" in audited.output
    assert "derivation-verified" not in audited.output
    restored = _by_path(plan_retention(run_root), wal_path(run_root, 1))
    assert restored.verdict == "prunable"
    assert restored.why == "the receipt is durable; completing the archive"


def test_pr55_a_swapped_checkpoint_under_a_receipt_refuses(tmp_path: Path) -> None:
    """PR-55: the receipt names the checkpoint that LICENSED the archive.

    A different one beside it is a swapped proof, and every verdict for
    that period would then be computed against an attestation nobody
    archived under. Absence is checked one test above; this is the case a
    presence check alone would let through."""
    from dsl41.period import archive_receipt_path

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    receipt = read_archive_receipt(run_root, 1)
    assert receipt is not None
    swapped = receipt.model_copy(update={"attestation_digest": "sha256:" + "ab" * 32})
    durable_write(str(archive_receipt_path(run_root, 1)), swapped.to_bytes())
    with pytest.raises(EngineError, match="is not the one that licensed the deletion"):
        plan_retention(run_root)


def test_pr55_an_archived_period_whose_proof_is_gone_refuses(tmp_path: Path) -> None:
    """PR-55: a receipt with no checkpoint beside it is not an archived
    period -- it is a period whose only remaining proof was deleted, and no
    re-derivation can replace it because the receipt says the inputs are
    gone."""
    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    attestation_path(run_root, 1).unlink()
    with pytest.raises(EngineError, match="its attestation is gone or does not hold together"):
        plan_retention(run_root)


def test_pr56_every_reader_names_the_archived_period_and_its_tier(tmp_path: Path) -> None:
    """PR-56: a multi-period archive, read by all four verbs.

    None of them may answer shorter in silence. `audit` reports the tier by
    name and shares no phrase with the derivation-verified line; `journal`
    prints an unreplayable gap ON STDOUT and crosses the next boundary on
    the checkpoint; `runs` names the coverage it lacks; `estate prune`
    re-plans the root without refusing."""
    from dsl41.boundary import walk_estate
    from dsl41.period import archive_receipt_path, archived_periods

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    assert archived_periods(run_root) == [1, 2]

    walk = walk_estate(default_anchor_dir(run_root))
    assert [entry.archived is not None for entry in walk.periods] == [True, True, False, False]

    audited = _invoke("audit", "--run-root", str(run_root))
    assert audited.exit_code == 0, audited.output
    lines = [line for line in audited.output.splitlines() if line.startswith("period ")]
    assert len(lines) == 3
    assert all("inputs archived, attestation-verified:" in line for line in lines[:2])
    assert "not re-derivable" in lines[0]
    assert lines[2].startswith("period 3 attested, derivation-verified:")
    assert "attestation-verified" not in lines[2]

    replayed = _invoke("journal", str(run_root))
    assert replayed.exit_code == 0, replayed.output
    assert replayed.stdout.count("UNREPLAYABLE GAP") == 2
    assert "period 3 sealed at index" in replayed.stdout  # it crossed and continued

    listed = _invoke("runs", str(run_root))
    assert listed.exit_code == 0
    assert listed.output.count("its inputs were archived") == 2

    swept = _invoke("estate", "prune", "--run-root", str(run_root), "--dry-run")
    assert swept.exit_code == 0, swept.output
    assert f"{archive_receipt_path(run_root, 1)}" in swept.output


def test_pr56_a_missing_segment_with_no_receipt_refuses_as_loss(tmp_path: Path) -> None:
    """PR-56: accidental loss must NEVER read as archiving.

    The same disk state -- a registered period with no segment -- is an
    archive with a receipt and a LOSS without one, and the receipt is
    written before any deletion exactly so the two can be told apart. All
    three readers refuse, each naming the receipt it did not find."""
    from dsl41.boundary import walk_estate
    from dsl41.period import archive_receipt_path

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    archive_receipt_path(run_root, 1).unlink()  # the archive becomes a loss

    with pytest.raises(EngineError, match="this is LOSS and not an archive"):
        walk_estate(default_anchor_dir(run_root))
    with pytest.raises(EngineError, match="this is LOSS and not an archive"):
        plan_retention(run_root)
    replayed = _invoke("journal", str(run_root))
    assert replayed.exit_code == 2
    assert "this is LOSS and not an archive" in replayed.output
    audited = _invoke("audit", "--run-root", str(run_root), "--period", "1")
    assert audited.exit_code == 2
    assert "this is LOSS and not an archive" in audited.output
    # and the loss is not quietly dropped from a WHOLE-root audit either:
    # a list built from "closed" alone would answer with a smaller estate
    whole = _invoke("audit", "--run-root", str(run_root))
    assert whole.exit_code == 2
    assert "this is LOSS and not an archive" in whole.output


def test_pr56_a_receipt_that_does_not_license_the_missing_file_is_not_a_licence(
    tmp_path: Path,
) -> None:
    """PR-56: the receipt has to name THIS file, not merely exist.

    A receipt whose list covers only a candidate pair does not excuse a WAL
    that went missing by accident, so the licence is checked against the
    LIST. A reader that took the receipt's presence as the answer would
    read exactly that loss as an archive -- and the list is what the whole
    artifact is for.

    Two disks are compared here and they differ in one file: period 3,
    which no receipt names at all, and period 2, whose receipt is rewritten
    to license only its candidate pair."""
    from dsl41.boundary import walk_estate
    from dsl41.period import archive_receipt_path

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    # (a) no receipt at all for period 3
    kept = wal_path(run_root, 3).read_bytes()
    wal_path(run_root, 3).unlink()
    with pytest.raises(EngineError, match="No archive receipt licenses its absence"):
        walk_estate(default_anchor_dir(run_root))
    durable_write(str(wal_path(run_root, 3)), kept)
    assert walk_estate(default_anchor_dir(run_root)).periods  # the estate is whole again

    # (b) a receipt NARROWED to the candidate pair is not a weaker
    # licence -- since B1 it is not a receipt at all. ss12a's archive is
    # all-or-nothing, so a list that drops the segment describes a state
    # this class never produces, and the artifact itself refuses before
    # any reader can weigh what it licenses
    receipt = read_archive_receipt(run_root, 2)
    assert receipt is not None and set(receipt.archived) == set(archivable_names(2))
    narrowed = receipt.model_copy(
        update={"archived": tuple(e for e in receipt.archived if not e.startswith("wal/"))}
    )
    durable_write(str(archive_receipt_path(run_root, 2)), narrowed.to_bytes())
    with pytest.raises(EngineError, match="ALL-OR-NOTHING"):
        walk_estate(default_anchor_dir(run_root))

    # (c) the licence check itself, asked of the door directly. A receipt
    # of the OTHER legal shape excuses its segment and nothing else, so a
    # reader excusing any other absence on it is refused by name
    durable_write(str(archive_receipt_path(run_root, 2)), receipt.to_bytes())
    from dsl41.attest import verify_archive_receipt

    segment_only = read_archive_receipt(run_root, 1)
    assert segment_only is not None and segment_only.archived == ("wal/000001.jsonl",)
    assert verify_archive_receipt(run_root, 1, licensing=wal_path(run_root, 1)) is not None
    with pytest.raises(EngineError, match="does not license it"):
        verify_archive_receipt(
            run_root, 1, licensing=period_dir(run_root, 1) / "candidate.json"
        )


def test_pr55_a_stranger_receipt_licenses_nothing(tmp_path: Path) -> None:
    """PR-55: a receipt authorizes a reader to accept an absence, so it is
    bound before it is believed -- this estate, this period, this seal, and
    the attestation this root actually holds."""
    from dsl41.period import archive_receipt_path

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    receipt = read_archive_receipt(run_root, 1)
    assert receipt is not None
    foreign = receipt.model_copy(update={"estate_id": "not-this-estate"})
    durable_write(str(archive_receipt_path(run_root, 1)), foreign.to_bytes())
    with pytest.raises(EngineError, match="a stranger's receipt excuses nothing here"):
        plan_retention(run_root)


def test_pr53_the_receipt_is_a_closed_artifact_like_every_other(tmp_path: Path) -> None:
    """PR-53, ss3.2: the receipt is read the way a seal and an attestation
    are read, because it decides the same kind of question.

    A stamped digest that does not match the bytes it stamps, a version
    this binary does not implement, and a second byte form of one logical
    artifact each refuse -- a receipt is what stands between an archive and
    a loss, so it is never read past."""
    from dsl41.period import ArchiveReceipt, archive_receipt_path

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    raw = archive_receipt_path(run_root, 1).read_bytes()
    receipt = ArchiveReceipt.from_bytes(raw, where="round trip")
    assert receipt == read_archive_receipt(run_root, 1)

    with pytest.raises(EngineError, match="disagrees with itself"):
        ArchiveReceipt.from_bytes(
            raw.replace(b'"retention_class":"archive-inputs"', b'"retention_class":"other-name"'),
            where="x",
        )
    with pytest.raises(EngineError, match="artifact_format_version"):
        ArchiveReceipt.from_bytes(
            raw.replace(b'"artifact_format_version":1', b'"artifact_format_version":2'),
            where="x",
        )
    with pytest.raises(EngineError, match="canonical serialization"):
        ArchiveReceipt.from_bytes(raw + b"\n", where="x")
    with pytest.raises(EngineError, match="not ss3.2-canonical JSON"):
        ArchiveReceipt.from_bytes(b"{,}", where="x")

    # the list is sorted, has no repeat, and is relative to the run root:
    # a retry completes from exactly it, so an absolute or traversing
    # entry would send the deletion somewhere the plan never proved
    for bad in (("b", "a"), ("a", "a"), ("/abs",), ("../out",), ()):
        with pytest.raises(ValueError):
            ArchiveReceipt.model_validate({**receipt.model_dump(mode="json"), "archived": bad})


def test_pr53_a_receipt_under_another_periods_filename_refuses(tmp_path: Path) -> None:
    """PR-53: a receipt licenses ONE period and is named for it. A file
    filed under another period's name is not that period's licence."""
    from dsl41.period import archive_receipt_path

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    receipt = read_archive_receipt(run_root, 1)
    assert receipt is not None
    durable_write(str(archive_receipt_path(run_root, 3)), receipt.to_bytes())
    with pytest.raises(EngineError, match="under another period's filename"):
        read_archive_receipt(run_root, 3)


def test_pr55_a_refused_segment_stops_every_later_one(tmp_path: Path) -> None:
    """PR-55: the retained segments are a contiguous suffix after every
    PARTIAL sweep, not only after a complete one.

    The archive deletes oldest-first for that reason. When the filesystem
    refuses period 1's segment, period 2's stays too -- deleting it would
    leave `wal/` holding 1 and 3 with a hole between them, which every
    segment-spanning reader would have to be taught to cross."""
    from dsl41.period import ARCHIVE_CLASS

    run_root = tmp_path / "run"
    _archivable(run_root)
    plan = plan_retention(run_root)
    assert [item.period_id for item in plan.prunable() if item.kind == "wal"] == [1, 2]
    import dsl41.retention as retention_mod

    real_remove = retention_mod._remove

    def refusing_the_oldest(plan_, item):
        if item.kind == "wal" and item.period_id == 1:
            raise OSError("the filesystem refused period 1's segment")
        real_remove(plan_, item)

    retention_mod._remove = refusing_the_oldest  # type: ignore[assignment]
    try:
        report = prune(plan, classes=(ARCHIVE_CLASS,), dry_run=False)
    finally:
        retention_mod._remove = real_remove  # type: ignore[assignment]

    assert wal_path(run_root, 1).exists() and wal_path(run_root, 2).exists()
    reasons = {str(item.path): why for item, why in report.failed}
    assert "the filesystem refused period 1's segment" in reasons[str(wal_path(run_root, 1))]
    assert "deletes oldest-first" in reasons[str(wal_path(run_root, 2))]
    assert [int(path.stem) for path in sorted((run_root / "wal").glob("*.jsonl"))] == [1, 2, 3, 4]


def test_pr56_the_subscriber_and_the_resume_survive_an_archive(tmp_path: Path) -> None:
    """PR-56: the two readers that are NOT reported on by `estate prune`.

    A REGRESSION guard rather than a rule pin, and it says so: no single
    mutation of the archive can make it red, because the two rules that
    keep these readers working -- the prefix and the oldest-first deletion
    -- are pinned by tests of their own. What this holds is the property
    those two rules exist FOR.

    The subscriber's backfill answers a cursor below the archive with
    ss11's gap marker, at the oldest RETAINED record -- which is the
    contract it already had, unchanged, and which holds only because the
    archived periods are a prefix and never a hole. And a live engine
    resumes over an archived lineage: recovery selects its seal by the
    SIDECAR the active segment names (ss11 step 3), and a sidecar is not a
    WAL."""
    from dsl41.runner_journal import read_backfill

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    opening = read_journal(wal_path(run_root, 3))[0]

    owed = read_backfill(run_root / "journal.jsonl", since=0)
    assert owed.gap_from == opening["first_index"]  # named, never streamed past
    assert owed.records and owed.records[0] == opening
    inside = read_backfill(run_root / "journal.jsonl", since=owed.gap_from + 1)
    assert inside.gap_from is None  # a cursor the root still retains: no gap

    _close(_resume(run_root, C2_JIL))


def test_a_root_that_is_not_an_estate_still_refuses_journal_by_name(tmp_path: Path) -> None:
    """DL-144 review: the archive taught `journal ROOT` to build its own
    segment list, and a list built by period NUMBER meets a root that has
    none.

    A directory with no periodized sentinel resolves to `journal.jsonl`
    itself, whose stem is not a number. Sorting on it turned a named
    refusal -- the thing a diagnosis surface owes -- into a traceback."""
    empty = tmp_path / "nothing"
    empty.mkdir()
    bare = _invoke("journal", str(empty))
    assert bare.exit_code == 2
    assert "journal" in bare.output

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "journal.jsonl").write_text('{"rec":"header","catalog_hash":"x"}\n')
    retired = _invoke("journal", str(legacy))
    assert retired.exit_code == 2
    assert "DL-138" in retired.output  # the retirement, by name -- not a traceback


def test_an_archive_refusal_is_not_also_reported_as_unselected(tmp_path: Path) -> None:
    """DL-144 review: `kept` is "prunable, outside the flags given", and a
    refused artifact is inside them.

    Reporting it in both buckets told the operator the sweep had both
    skipped it for want of a flag and refused it with one, which is two
    contradictory answers about one file."""
    from dsl41.period import ARCHIVE_CLASS, attestation_path as attest_path

    run_root = tmp_path / "run"
    _archivable(run_root)
    plan = plan_retention(run_root)
    for period_id in (2, 3):
        attest_path(run_root, period_id).write_bytes(b'{"artifact_format_version":1}\n')
    report = prune(plan, classes=(ARCHIVE_CLASS,), dry_run=False)
    assert report.refused
    refused = {(str(item.path), item.kind) for item, _ in report.refused}
    assert refused
    assert not refused & {(str(item.path), item.kind) for item in report.kept}


def test_pr56_readers_agree_in_the_receipt_written_crash_window(tmp_path: Path) -> None:
    """DL-144 review: three readers, one disk, one story.

    In the window the design names -- receipt durable, deletions not done
    -- the segment is still there and `runs` still folds its rows. A
    warning taken from the receipt alone said those rows were gone while
    printing them. What the readers owe here is the opposite: name the
    TIER (which the receipt decides) and do not claim a coverage gap that
    is not there (which the disk decides)."""
    from dsl41.period import ARCHIVE_CLASS

    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _run_job(engine, "b")
    effect = _spawn_effects(run_root, 1)[0]
    _tombstone(run_root, effect)
    for boundary_no in range(1, 4):
        text = [C2_JIL, C3_JIL][(boundary_no - 1) % 2]
        asyncio.run(
            _seal(engine, _request(engine, _stage(run_root, text), request_id=f"r-{boundary_no}"))
        )
        _close(engine)
        engine = _resume(run_root, text)
    _close(engine)
    for period_id in (1, 2, 3):
        _attest(run_root, period_id)
    prune(plan_retention(run_root), classes=("tombstones",), dry_run=False)

    before = _invoke("runs", str(run_root))
    assert before.exit_code == 0

    import dsl41.retention as retention_mod

    real_remove = retention_mod._remove
    retention_mod._remove = lambda plan, item: (_ for _ in ()).throw(OSError("power loss"))
    try:
        prune(plan_retention(run_root), classes=(ARCHIVE_CLASS,), dry_run=False)
    finally:
        retention_mod._remove = real_remove  # type: ignore[assignment]
    assert wal_path(run_root, 1).exists()  # the window: receipt yes, deletion no

    listed = _invoke("runs", str(run_root))
    assert listed.exit_code == 0
    # the fold is over the same segments, so it is the same answer -- and
    # a warning that claimed a coverage gap would be describing a table
    # that did not change
    assert listed.stdout == before.stdout
    assert "its inputs were archived" not in listed.output

    replayed = _invoke("journal", str(run_root))
    assert replayed.exit_code == 0, replayed.output
    assert "UNREPLAYABLE GAP" not in replayed.stdout  # it is replayable, and is replayed
    assert "inputs ARCHIVED" in replayed.stdout
    assert "attestation-verified tier" in replayed.stdout

    audited = _invoke("audit", "--run-root", str(run_root), "--period", "1")
    assert audited.exit_code == 0
    assert "inputs archived, attestation-verified:" in audited.output


def test_a_receipt_of_an_unknown_class_licenses_nothing_in_any_reader(tmp_path: Path) -> None:
    """DL-144 review: the class was bound in the PLANNER alone, so the
    walk, `audit` and `journal` all honoured a receipt written by a policy
    this binary does not implement while `estate prune` refused it.

    A reader that accepts an absence under rules it cannot check is the
    one asymmetry this artifact must not have -- so the binding sits in
    `read_archive_receipt`, the door every reader goes through."""
    from dsl41.boundary import walk_estate
    from dsl41.period import archive_receipt_path

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    receipt = read_archive_receipt(run_root, 1)
    assert receipt is not None
    stranger = receipt.model_copy(update={"retention_class": "some-future-policy"})
    durable_write(str(archive_receipt_path(run_root, 1)), stranger.to_bytes())

    for reader in (
        lambda: read_archive_receipt(run_root, 1),
        lambda: walk_estate(default_anchor_dir(run_root)),
        lambda: plan_retention(run_root),
    ):
        with pytest.raises(EngineError, match="retention class 'some-future-policy'"):
            reader()
    for args in (
        ("journal", str(run_root)),
        ("audit", "--run-root", str(run_root), "--period", "1"),
    ):
        result = _invoke(*args)
        assert result.exit_code == 2, (args, result.output)
        assert "some-future-policy" in result.output, args


# ============================ DL-144 review round two: B1..B4


def test_b1_the_receipt_permits_two_shapes_and_no_others(tmp_path: Path) -> None:
    """B1: ss12a's archive is ALL-OR-NOTHING, and the ARTIFACT says so.

    A receipt licensing a candidate pair without the segment -- or another
    period's files, or a path of no shape this class deletes -- describes a
    state the class never produces. Left to the verb that writes it,
    "archived" would be a spectrum rather than the one crisp state every
    reader reports a TIER from, and a hand-edited receipt would move a
    period onto it."""
    from dsl41.period import ArchiveReceipt, archivable_names

    wal, candidate, staged = archivable_names(2)
    body = {
        "estate_id": "e",
        "period_id": 2,
        "seal_digest": "sha256:" + "a" * 64,
        "attestation_digest": "sha256:" + "b" * 64,
        "chain_through_period": 3,
        "retention_class": "archive-inputs",
        "archived_at": "2026-08-21T10:00:00.000000",
        "dsl41_version": "0.1.0",
    }
    for legal in ((wal,), tuple(sorted((wal, candidate, staged)))):
        assert ArchiveReceipt(**body, archived=legal).archived == legal
    for illegal in (
        (candidate, staged),  # the pair without the segment
        (candidate,),
        (staged,),
        ("wal/000003.jsonl",),  # another period's segment
        ("periods/000003/candidate.json", "periods/000003/staged_manifest.json", wal),
        (candidate, "logs/j.1.out", staged, wal),  # a kind the class never deletes
        (wal, "wal/000002.jsonl.bak"),
    ):
        with pytest.raises(ValidationError, match="ALL-OR-NOTHING"):
            ArchiveReceipt(**body, archived=tuple(sorted(illegal)))


def _corrupt_receipt(run_root: Path, period_id: int, **fields: Any) -> None:
    """Rewrite one period's receipt with `fields` changed, re-stamping its
    digest so the artifact still agrees with itself.

    The point of every B2 case: integrity is not the property under test.
    A receipt that digests correctly and names the wrong PROOF is what a
    consumer trusting the file rather than the binding would honour."""
    from dsl41.period import archive_receipt_path

    receipt = read_archive_receipt(run_root, period_id)
    assert receipt is not None
    durable_write(
        str(archive_receipt_path(run_root, period_id)),
        receipt.model_copy(update=fields).to_bytes(),
    )


def _restamp(**fields: Any):
    """One receipt field changed and the artifact re-stamped."""

    def apply(run_root: Path, tmp_path: Path) -> None:
        _corrupt_receipt(run_root, 1, **fields)

    return apply


def _foreign_pair(run_root: Path, tmp_path: Path) -> None:
    """B5: a CORRELATED forgery -- a real second genesis's seal and
    attestation for period 1, dropped in, with the receipt re-stamped onto
    them.

    Every binding the door had before B5 agrees with this disk: the
    receipt names that sidecar, that sidecar carries that checkpoint, the
    checkpoint chains through this period, and the receipt claims this
    estate. `read_seal` parses a sidecar and never asks WHOSE, so the one
    fact none of them states is that the pair belongs to another lineage.

    The pair is produced by a real genesis and a real `audit`, never hand
    written: a forged pair that could not have been produced would prove
    only that some validator rejects it."""
    from dsl41.attest import read_attestation

    stranger = tmp_path / "stranger-pair"
    _periods(stranger, 3, attest_through=2)
    for source, target in (
        (seal_path(stranger, 1), seal_path(run_root, 1)),
        (attestation_path(stranger, 1), attestation_path(run_root, 1)),
    ):
        durable_write(str(target), source.read_bytes())
    foreign_seal = read_seal(run_root, 1)
    foreign_attestation = read_attestation(run_root, 1)
    assert foreign_attestation is not None
    assert foreign_seal.estate_id != read_sentinel(run_root).estate_id
    _corrupt_receipt(
        run_root,
        1,
        seal_digest=foreign_seal.digest,
        attestation_digest=foreign_attestation.digest,
        chain_through_period=foreign_attestation.chain_through_period,
    )


#: every way one receipt can fail to be the proof it claims, and the
#: fragment the shared door answers each with. Integrity is intact in all
#: of them -- what is broken is the BINDING
B2_CORRUPTIONS: Any = [
    ("seal", _restamp(seal_digest="sha256:" + "cd" * 32), "is not this boundary's"),
    (
        "attestation",
        _restamp(attestation_digest="sha256:" + "ef" * 32),
        "is not the one that licensed the deletion",
    ),
    (
        "chain",
        _restamp(chain_through_period=9),
        "disagree about how far the induction reached",
    ),
    (
        "estate",
        _restamp(estate_id="some-other-estate"),
        "a stranger's receipt excuses nothing",
    ),
    ("foreign-pair", _foreign_pair, "a foreign sidecar under this period's name"),
]


@pytest.mark.parametrize("name,corrupt,fragment", B2_CORRUPTIONS)
def test_b2_every_receipt_consumer_refuses_the_same_broken_binding(
    tmp_path: Path, name: str, corrupt: Any, fragment: str
) -> None:
    """B2: ONE door, and every reader that treats a receipt as authority
    goes through it.

    Round one bound the receipt in the PLANNER alone. So a receipt naming
    a proof this root does not hold shortened `runs` output, narrated a
    gap in `journal`, reported the weaker tier in `audit` and resolved a
    registry row in the walk -- while `estate prune` refused the very same
    file. Four readers agreeing on nothing is worse than any one of them
    being wrong, because it makes the estate's answer depend on which verb
    an operator happened to type."""
    from dsl41.attest import verified_tier
    from dsl41.boundary import walk_estate

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    corrupt(run_root, tmp_path)

    for reader in (
        lambda: read_archive_receipt(run_root, 1) and walk_estate(default_anchor_dir(run_root)),
        lambda: plan_retention(run_root),
        lambda: verified_tier(run_root, 1),
    ):
        with pytest.raises(EngineError, match=fragment):
            reader()
    for args in (
        ("journal", str(run_root)),
        ("runs", str(run_root)),
        ("audit", "--run-root", str(run_root)),
        ("estate", "prune", "--run-root", str(run_root), "--dry-run"),
    ):
        result = _invoke(*args)
        assert result.exit_code == 2, (name, args, result.output)
        assert fragment in result.output, (name, args, result.output)
        # and NOTHING was answered shorter: no verb printed a table, a
        # trace or a verdict list over an estate it could not prove
        assert "UNREPLAYABLE GAP" not in result.stdout, (name, args)
        assert "attestation-verified" not in result.stdout, (name, args)


def test_b2_an_unreadable_receipt_never_buys_a_shorter_answer(tmp_path: Path) -> None:
    """B2: `archived_periods` lists receipt FILES, which is what a lister
    owes -- and `dsl41 runs` used it to decide there were no rows.

    A root whose every period is archived answers with an empty table. A
    root whose receipt is unreadable must not: that is the same empty
    table bought with a file nobody checked, and it is indistinguishable
    from the honest one."""
    from dsl41.period import archive_receipt_path, archived_periods

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    durable_write(str(archive_receipt_path(run_root, 1)), b"{not canonical json\n")
    assert archived_periods(run_root) == [1, 2]  # the LISTER still lists it

    for args in (("runs", str(run_root)), ("journal", str(run_root))):
        result = _invoke(*args)
        assert result.exit_code == 2, (args, result.output)
        assert "000001.archive.json" in result.output, args
    with pytest.raises(EngineError, match="not ss3.2-canonical JSON"):
        plan_retention(run_root)


def test_b3_a_registry_row_pointing_at_a_stranger_releases_nothing(tmp_path: Path) -> None:
    """B3, the severe one: the cover came from a checkpoint found through
    an UNVALIDATED registry root.

    The chain checkpoint that covers a period may live in another root
    after a roll, so the cover has to be an estate fact. Round one read
    the row, went to the directory it named and verified whatever
    attestation was there -- so a row edited to point at ANOTHER ESTATE's
    root supplied an internally-valid later checkpoint and released THIS
    estate's WAL. An unproved root authorizing a deletion is the one thing
    the floor exists to prevent.

    The stranger here is a real second genesis with a real audited period,
    so its checkpoint verifies on its own terms exactly as the attack
    needs."""
    from dsl41.boundary import EstateAnchor, PeriodRow
    from dsl41.period import read_sentinel

    run_root = tmp_path / "run"
    _archivable(run_root)
    assert [item.period_id for item in plan_retention(run_root).prunable() if item.kind == "wal"]

    stranger = tmp_path / "stranger"
    _periods(stranger, 3, attest_through=2)
    assert attestation_path(stranger, 2).exists()
    assert read_sentinel(stranger).estate_id != read_sentinel(run_root).estate_id

    anchor = EstateAnchor(default_anchor_dir(run_root))
    anchor.acquire()
    try:
        stored = anchor.require()
        rows = dict(stored.periods)
        rows["3"] = PeriodRow(**{**rows["3"].model_dump(), "root": str(stranger)})
        anchor.write(stored.model_copy(update={"periods": rows}))
    finally:
        anchor.release()

    with pytest.raises(EngineError, match="resolves to a stranger's root proves nothing"):
        plan_retention(run_root)
    swept = _invoke("estate", "prune", "--run-root", str(run_root), "--archive-inputs")
    assert swept.exit_code == 2
    assert "resolves to a stranger's root proves nothing" in swept.output
    assert wal_path(run_root, 1).exists() and wal_path(run_root, 2).exists()


def test_b3_the_live_recheck_asks_the_same_question_as_the_plan(tmp_path: Path) -> None:
    """B3: the re-check is what stands between a plan and a deletion, so a
    row redirected AFTER planning must not supply the cover that licenses
    it.

    The plan is taken while the registry is honest and the row is moved
    before the sweep runs -- exactly the window a re-check exists for."""
    from dsl41.boundary import EstateAnchor, PeriodRow
    from dsl41.period import ARCHIVE_CLASS, archive_receipt_path

    run_root = tmp_path / "run"
    _archivable(run_root)
    plan = plan_retention(run_root)
    assert [item.period_id for item in plan.prunable() if item.kind == "wal"] == [1, 2]

    stranger = tmp_path / "stranger"
    _periods(stranger, 3, attest_through=2)
    anchor = EstateAnchor(default_anchor_dir(run_root))
    anchor.acquire()
    try:
        stored = anchor.require()
        rows = dict(stored.periods)
        rows["3"] = PeriodRow(**{**rows["3"].model_dump(), "root": str(stranger)})
        anchor.write(stored.model_copy(update={"periods": rows}))
    finally:
        anchor.release()

    report = prune(plan, classes=(ARCHIVE_CLASS,), dry_run=False)
    assert report.removed == ()
    assert report.refused
    assert all("stranger's root" in why for _, why in report.refused), report.refused
    assert not archive_receipt_path(run_root, 1).exists()
    assert wal_path(run_root, 1).exists()


def test_b4_a_half_deleted_candidate_pair_is_completed_from_the_receipt(
    tmp_path: Path,
) -> None:
    """B4: the receipt is the RECOVERY authority, and enumeration comes
    from it.

    Crash after `candidate.json` goes and before `staged_manifest.json`
    does, and the ordinary derivation says there is no candidate in this
    period at all -- `read_candidate` returns None and returns early. The
    staged file was then listed by no plan, held by no floor and reachable
    by no verb: an artifact the estate could neither keep on purpose nor
    finish removing. Enumerating from the receipt closes it, because that
    is what a point of no return means."""
    from dsl41.boundary import read_candidate
    from dsl41.period import ARCHIVE_CLASS

    run_root = tmp_path / "run"
    _archivable(run_root)
    candidate = period_dir(run_root, 2) / "candidate.json"
    staged = period_dir(run_root, 2) / "staged_manifest.json"
    assert candidate.exists() and staged.exists()

    import dsl41.retention as retention_mod

    real_remove = retention_mod._remove

    def halfway(plan, item):
        # the first removal lands, the second is the power cut
        if item.path == staged:
            raise OSError("power loss between the pair")
        real_remove(plan, item)

    retention_mod._remove = halfway  # type: ignore[assignment]
    try:
        first = prune(plan_retention(run_root), classes=(ARCHIVE_CLASS,), dry_run=False)
    finally:
        retention_mod._remove = real_remove  # type: ignore[assignment]
    assert not candidate.exists() and staged.exists()
    assert any(item.path == staged for item, _ in first.failed)
    assert read_candidate(period_dir(run_root, 2)) is None  # the derivation is blind now

    resumed = plan_retention(run_root)
    left = _by_path(resumed, staged)
    assert left.verdict == "prunable" and left.rule == "DL-144"
    assert left.why == "the receipt is durable; completing the archive"
    finished = prune(resumed, classes=(ARCHIVE_CLASS,), dry_run=False)
    assert finished.refused == () and finished.failed == ()
    assert not staged.exists()
    assert not wal_path(run_root, 2).exists()


def test_b6_audit_period_and_rederive_seal_refuse_a_receipt_themselves(
    tmp_path: Path,
) -> None:
    """B6: the two FUNCTIONS, not the CLI path in front of them.

    `audit_period` read the receipt directly and used it to skip the loss
    branch and return an attestation. The CLI missed that because
    `verified_tier` refuses one frame earlier -- but a caller is not a
    guard, and a library function that is only safe behind one of its
    callers is not safe. `rederive_seal` had the same read, deciding
    whether a missing WAL reads as ARCHIVED or as a rolled root.

    Both are driven here with no CLI in the way, over a receipt whose
    integrity is intact and whose binding is not."""
    from dsl41.attest import audit_period, rederive_seal

    run_root = tmp_path / "run"
    _archivable(run_root)
    _archive(run_root)
    _corrupt_receipt(run_root, 1, attestation_digest="sha256:" + "ef" * 32)

    with pytest.raises(EngineError, match="is not the one that licensed the deletion"):
        audit_period(run_root, 1, anchor=_anchor(run_root))
    with pytest.raises(EngineError, match="is not the one that licensed the deletion"):
        rederive_seal(run_root, 1)

    # and the honest estate still works through both, so this is a gate
    # rather than a build in which the two never succeed
    durable_write(
        str(archive_receipt_path(run_root, 1)),
        read_archive_receipt(run_root, 1).model_copy(
            update={"attestation_digest": verify_attestation(run_root, 1).digest}
        ).to_bytes(),
    )
    assert audit_period(run_root, 1, anchor=_anchor(run_root)).period_id == 1
    with pytest.raises(EngineError, match="inputs were ARCHIVED"):
        rederive_seal(run_root, 1)  # archived, and it says so rather than "not in this root"


def test_b7_an_unsealed_row_with_an_orphan_sidecar_is_skipped_not_refused(
    tmp_path: Path,
) -> None:
    """B7's other half: the row's digest decides whether a period is a
    COVER CANDIDATE at all, and an unsealed one is skipped.

    ss11 names the state: a crash after the sidecar is durable and before
    the record that commits it leaves an ORPHAN sidecar, and an orphan is
    never selected. Reaching the digest comparison with `row.seal_digest`
    still null would read that ordinary crash window as "a sidecar this
    lineage never closed with" and refuse every prune of the estate until
    an operator went looking for corruption that is not there.

    So the guard is a rule, not a short-circuit: skip the row, plan the
    rest, and let the cover come from a period that is actually sealed."""
    from dsl41.boundary import PeriodRow

    run_root = tmp_path / "run"
    _archivable(run_root)
    before = plan_retention(run_root)
    assert [item.period_id for item in before.prunable() if item.kind == "wal"] == [1, 2]
    store = _anchor(run_root)
    committed = store.require().row(3)
    assert committed is not None and committed.seal_digest == read_seal(run_root, 3).digest

    store.acquire()
    try:
        stored = store.require()
        rows = dict(stored.periods)
        # the crash window: period 3's sidecar is durable, the row that
        # commits it is not
        rows["3"] = PeriodRow(**{**committed.model_dump(), "seal_digest": None})
        store.write(stored.model_copy(update={"periods": rows}))
    finally:
        store.release()

    plan = plan_retention(run_root)  # plans, rather than refusing
    # period 3 is no longer a cover candidate, so the cover falls back to
    # period 2 -- and period 2 itself is then held, NAMED, never offered
    # on a seal the lineage has not committed
    assert _by_path(plan, wal_path(run_root, 1)).verdict == "prunable"
    held = _by_path(plan, wal_path(run_root, 2))
    assert held.verdict == "held"
    assert "no chain checkpoint above period 2 covers it" in held.why
