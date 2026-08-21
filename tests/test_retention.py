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

from dsl41.attest import audit_period
from dsl41.boundary import (
    ClaimedHead,
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
    attestation_path,
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
        assert entry.verdict == "held"
        assert entry.why == "the seal that installed this candidate has committed"


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
    assert superseded.why == "checkpoint 2 covers it by induction"
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
    """ss12 floors the WAL of any unattested period, and gates the rest on
    E20 -- may a seal-only archive stand in for pruned inputs?

    Until that is answered an attested period's WAL is HELD rather than
    offered: it is the input the open question is about. The pin exists so
    that answering E20 is a deliberate change here and not a drift."""
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    plan = plan_retention(run_root)
    attested = _by_path(plan, wal_path(run_root, 1))
    assert attested.verdict == "held" and attested.rule == "PR-Q3"
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
