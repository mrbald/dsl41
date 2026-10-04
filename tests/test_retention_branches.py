"""Branch tests for `dsl41.retention` that the floors, archive and prune
suites do not reach (DL-269).

Normative spec: `docs/period-model.md` ss1.1, ss1.3, ss11a, ss12 and ss12a;
DL-135 (the plan and its observation snapshot) and DL-144 (the archive).
The estates are built by the fixtures test_retention.py already shares from
test_boundary.py; a refusal has a twin that does not trigger it.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_boundary import (
    C2_JIL,
    T0,
    _catalog,
    _close,
    _genesis,
    _request,
    _resume,
    _seal,
    _stage,
)
from test_retention import (
    _anchor,
    _archivable,
    _archive,
    _attest,
    _attested_tombstone_root,
    _by_path,
    _open_period_one,
    _periods,
    _run_job,
    _spawn_effects,
    _tombstone,
)

import dsl41.retention as retention_mod
from dsl41.boundary import EstateAnchor, PeriodRow, default_anchor_dir
from dsl41.canon import canonical_bytes
from dsl41.oracle_state import Event
from dsl41.period import (
    ARCHIVE_CLASS,
    archive_receipt_path,
    attestation_path,
    read_sentinel,
    seal_path,
    wal_path,
)
from dsl41.retention import Artifact, plan_retention, prune
from dsl41.runner_clock import EngineError
from dsl41.runner_procid import durable_write


def _edit_row(run_root: Path, period_id: int, **fields: Any) -> None:
    """Rewrite one registry row of the root's anchor, under its lock."""
    anchor = _anchor(run_root)
    anchor.acquire()
    try:
        stored = anchor.read()
        assert stored is not None
        row = stored.periods[str(period_id)].model_copy(update=fields)
        anchor.write(stored.model_copy(update={"periods": {**stored.periods, str(period_id): row}}))
    finally:
        anchor.release()


# ------------------------------------------------------------ plan_retention


def test_ss1_3_a_root_with_a_sentinel_and_no_anchor_refuses_to_plan(tmp_path: Path) -> None:
    """ss1.3, ss12: the lineage head says which artifacts are reachable, so
    a plan with no anchor is refused, not computed over nothing."""
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    plan_retention(run_root)  # the twin: the real anchor plans
    with pytest.raises(EngineError, match=r"no anchor -- the lineage head"):
        plan_retention(run_root, anchor_dir=tmp_path / "no-anchor-here")


def test_ss1_1_a_sentinel_with_no_segment_and_no_receipt_is_an_interrupted_genesis(
    tmp_path: Path,
) -> None:
    """ss1.1: a root whose `wal/` holds no segment and no archive receipt is
    not an estate to prune. The registry row is provisional here (genesis
    inserts it before the segment is durable), so the absence is not LOSS;
    with a durable row the same absence is refused as loss instead."""
    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _close(engine)
    wal_path(run_root, 1).unlink()
    with pytest.raises(EngineError, match="this LOSS and not an archive|is LOSS"):
        plan_retention(run_root)
    _edit_row(run_root, 1, segment_durable=False)
    with pytest.raises(EngineError, match="an interrupted genesis, not an estate to prune"):
        plan_retention(run_root)


# ------------------------------------------------- the observation snapshot


class _FakeEntry:
    """A directory entry whose `is_dir` fails, standing for a disk error
    between the listing and the type query."""

    name = "child"

    def inode(self) -> int:
        return 1

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        raise OSError("EIO: is_dir")


def test_ss12_a_directory_that_cannot_be_listed_refuses_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ss12: the snapshot is fail-closed on every error. A failure to list
    an opened directory is "estate unreadable or mutating"."""
    (tmp_path / "estate").mkdir()
    real = os.scandir

    def failing(target: Any = None) -> Any:
        if isinstance(target, int):  # the descriptor form is the snapshot's alone
            raise PermissionError("EACCES: scandir")
        return real(target)

    monkeypatch.setattr(os, "scandir", failing)
    with pytest.raises(EngineError, match=r"estate unreadable or mutating .*EACCES"):
        retention_mod._snapshot_idents(tmp_path / "estate")
    monkeypatch.setattr(os, "scandir", real)
    assert tmp_path / "estate" in retention_mod._snapshot_idents(tmp_path / "estate")  # twin


def test_ss12_an_entry_whose_type_cannot_be_read_refuses_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "estate").mkdir()
    real = os.scandir

    def listing(target: Any = None) -> Any:
        if isinstance(target, int):
            return [_FakeEntry()]
        return real(target)

    monkeypatch.setattr(os, "scandir", listing)
    with pytest.raises(EngineError, match=r"child: estate unreadable or mutating .*EIO"):
        retention_mod._snapshot_idents(tmp_path / "estate")


def test_ss12_a_root_that_cannot_be_opened_refuses_and_an_absent_one_is_skipped(
    tmp_path: Path,
) -> None:
    """Only ENOENT is skipped (an anchor directory need not exist). A path
    that exists and is not a directory is an error, never an empty tree."""
    assert retention_mod._snapshot_idents(tmp_path / "absent") == {}
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("x")
    with pytest.raises(EngineError, match="estate unreadable or mutating"):
        retention_mod._snapshot_idents(not_a_directory)


# ---------------------------------------------------- pinning a retained tree


def test_ss12_a_retained_artifact_that_vanished_refuses_the_plan(tmp_path: Path) -> None:
    """Every artifact on the list was seen on disk moments earlier, so an
    absence at pin time is concurrent mutation, and fail-closed."""
    with pytest.raises(EngineError, match="retained artifact unreadable"):
        retention_mod._idents_under(tmp_path / "gone")


def test_ss12_a_retained_file_pins_itself_alone(tmp_path: Path) -> None:
    """The twin of the tree cases: a file has no children to pin."""
    target = tmp_path / "file"
    target.write_text("x")
    assert retention_mod._idents_under(target) == [(target.lstat().st_dev, target.lstat().st_ino)]


def test_ss12_an_unreadable_subdirectory_of_a_retained_tree_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping an unreadable subdirectory would leave its children
    unpinned, so the walk's own error refuses the plan."""
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "leaf").write_text("x")
    assert len(retention_mod._idents_under(tree)) == 3  # the twin: all three pinned
    real = os.scandir

    def failing(target: Any = None) -> Any:
        if str(target) == str(tree / "sub"):
            raise PermissionError(13, "EACCES", str(tree / "sub"))
        return real(target)

    monkeypatch.setattr(os, "scandir", failing)
    with pytest.raises(EngineError, match=r"sub: retained tree unreadable"):
        retention_mod._idents_under(tree)


def test_ss12_an_entry_that_vanishes_between_listing_and_lstat_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unlinked inode is harmless and a renamed one is live and
    unpinned; the two cannot be told apart, so the walk refuses."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "leaf").write_text("x")
    real = os.lstat

    def failing(target: Any, *args: Any, **kwargs: Any) -> Any:
        if str(target) == str(tree / "leaf"):
            raise FileNotFoundError(2, "vanished", str(target))
        return real(target, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", failing)
    with pytest.raises(EngineError, match=r"leaf: retained tree unreadable"):
        retention_mod._idents_under(tree)


def test_ss12_a_stamp_is_the_observed_identity_only_while_the_disk_agrees(
    tmp_path: Path,
) -> None:
    """A path the snapshot never saw is stamped with nothing (no deletion
    is licensed for it), and an unreadable one has no identity."""
    seen = tmp_path / "seen"
    seen.write_text("x")
    ident = (seen.lstat().st_dev, seen.lstat().st_ino)
    assert retention_mod._snapshot_stamp({seen: ident}, seen) == ident
    assert retention_mod._snapshot_stamp({}, seen) is None  # never observed
    assert retention_mod._snapshot_stamp({seen: (0, 0)}, seen) is None  # the disk disagrees
    assert retention_mod._lstat_ident(tmp_path / "gone") is None


# ------------------------------------------------------- the covering walk


def _alt_root(tmp_path: Path, run_root: Path) -> Path:
    """A second root of the SAME estate: the sentinel is copied, and the
    caller adds what it wants the registry row to find there."""
    alt = tmp_path / "alt"
    (alt / "seals").mkdir(parents=True)
    shutil.copyfile(run_root / "journal.jsonl", alt / "journal.jsonl")
    return alt


def _row(root: Path, digest: str | None = "sha256:" + "1" * 64) -> PeriodRow:
    return PeriodRow(root=str(root), segment_durable=True, seal_digest=digest)


def test_ss1_3_a_registered_root_that_is_off_line_proves_nothing_and_is_skipped(
    tmp_path: Path,
) -> None:
    """ss1.3: "I cannot prove a cover from here" only ever holds more. A
    row whose root is missing is skipped, not refused."""
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    estate = read_sentinel(run_root).estate_id  # type: ignore[union-attr]
    assert retention_mod._covered_through(estate, [(1, _row(tmp_path / "off-line"))]) is None


def test_ss1_3_a_sidecar_that_will_not_parse_binds_no_checkpoint(tmp_path: Path) -> None:
    """A registered root whose sidecar is garbage covers nothing and the
    walk keeps looking further down."""
    run_root = tmp_path / "run"
    _periods(run_root, 3, attest_through=2)
    estate = read_sentinel(run_root).estate_id  # type: ignore[union-attr]
    alt = _alt_root(tmp_path, run_root)
    (alt / "seals" / "000002.json").write_bytes(b"not a sidecar")
    attestation_path(alt, 2).write_bytes(b"{}")
    sealed = _anchor(run_root).read().periods["1"].seal_digest  # type: ignore[union-attr]
    rows = [(1, _row(run_root, sealed)), (2, _row(alt))]
    # period 2's sidecar binds nothing, so the walk goes on down to period 1,
    # whose own root proves it
    assert retention_mod._covered_through(estate, rows) == 1


def test_ss1_3_a_foreign_pair_in_a_registered_root_refuses(tmp_path: Path) -> None:
    """ss12: a root the registry names, holding a sidecar of ANOTHER estate
    or of another period under this period's filename, is an edited row
    and refuses. A sidecar that is this estate's and this period's passes
    on to the next binding."""
    run_root = tmp_path / "run"
    _periods(run_root, 3, attest_through=2)
    estate = read_sentinel(run_root).estate_id  # type: ignore[union-attr]
    other = tmp_path / "other"
    _periods(other, 2, attest_through=1)
    alt = _alt_root(tmp_path, run_root)
    attestation_path(alt, 1).write_bytes(b"{}")
    shutil.copyfile(seal_path(other, 1), seal_path(alt, 1))  # another estate's
    with pytest.raises(EngineError, match="a foreign pair in a registered root"):
        retention_mod._covered_through(estate, [(1, _row(alt))])

    shutil.copyfile(seal_path(run_root, 1), seal_path(alt, 2))  # period 1 under 2's name
    attestation_path(alt, 2).write_bytes(b"{}")
    with pytest.raises(EngineError, match="a foreign pair in a registered root"):
        retention_mod._covered_through(estate, [(2, _row(alt))])

    shutil.copyfile(seal_path(run_root, 1), seal_path(alt, 1))  # the twin: this estate's
    digest = _anchor(run_root).read().periods["1"].seal_digest  # type: ignore[union-attr]
    assert retention_mod._covered_through(estate, [(1, _row(alt, digest))]) is None


# --------------------------------------------------------- the sidecars held


def test_ss12_a_sidecar_attesting_another_period_under_this_ones_name_refuses(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    plan_retention(run_root)  # the twin: the root as built plans
    shutil.copyfile(seal_path(run_root, 1), seal_path(run_root, 2))
    with pytest.raises(EngineError, match=r"attests period 1 under another period's filename"):
        plan_retention(run_root)


def test_ss11_a_successor_that_opened_from_another_sidecar_refuses(tmp_path: Path) -> None:
    """ss11, ss12: on a rolled root the successor's `opens_from_seal` is the
    only binding of the imported sidecar, and a digest that is not the
    sidecar's is a replaced pair."""
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
    plan_retention(root_b, anchor_dir=anchor_dir)  # the twin: the link names the sidecar

    segment = wal_path(root_b, 2)
    lines = segment.read_bytes().splitlines()
    opening = json.loads(lines[0])
    opening["opens_from_seal"]["digest"] = "sha256:" + "0" * 64
    segment.write_bytes(b"\n".join([canonical_bytes(opening), *lines[1:]]) + b"\n")
    with pytest.raises(EngineError, match="the successor opened from"):
        plan_retention(root_b, anchor_dir=anchor_dir)


# ------------------------------------------------- bundles and the run index


def test_ss12_a_root_with_no_catalogs_directory_has_no_bundle_verdicts(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    _periods(run_root, 2, attest_through=1)
    assert any(item.kind == "bundle" for item in plan_retention(run_root).artifacts)  # the twin
    shutil.rmtree(run_root / "catalogs")
    assert not any(item.kind == "bundle" for item in plan_retention(run_root).artifacts)


def test_ss11a_only_spawn_effects_bind_a_run_to_its_period(tmp_path: Path) -> None:
    """ss11a: a KILL is an effect of the run a SPAWN already bound, and it
    names no birth. A run whose SPAWN is decided in period 1 (held for a
    drained host), carried across the seal, started and then killed in
    period 2 has a KILL in period 2's WAL with the SPAWN's job, run_number
    and run_id. The run stays bound to period 1, and the plan does not read
    the later KILL as a second birth (I2)."""
    from dsl41.runner_hosts import HostCommand
    from dsl41.runner_journal import read_journal

    run_root = tmp_path / "run"
    engine = _genesis(run_root)

    async def hold_a_spawn() -> None:
        engine.inject_host(HostCommand(verb="drain", host_id=engine.executor_id))
        await engine.run_until_quiescent(T0)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "a"}))
        await engine.run_until_quiescent(T0)

    asyncio.run(hold_a_spawn())
    [spawn] = [e for e in engine.outbox.pending() if e.kind == "SPAWN"]
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)

    opened = _resume(run_root, C2_JIL)

    async def start_then_kill_in_period_two() -> None:
        opened.inject_host(HostCommand(verb="activate", host_id=opened.executor_id))
        await opened.run_until_quiescent(T0 + timedelta(minutes=1))
        opened.inject(Event(at=T0 + timedelta(minutes=2), kind="KILLJOB", payload={"job": "a"}))
        await opened.run_until_quiescent(T0 + timedelta(minutes=3))

    asyncio.run(start_then_kill_in_period_two())
    _close(opened)

    kills = [
        effect
        for record in read_journal(wal_path(run_root, 2))
        if record.get("rec") == "decision"
        for effect in record.get("effects") or ()
        if effect["kind"] == "KILL"
    ]
    assert [(k["job"], k["run_number"], k["run_id"]) for k in kills] == [("a", 1, spawn.run_id)]
    assert retention_mod._spawn_periods(run_root, [1, 2]) == {("a", 1): (1, spawn.run_id)}
    plan_retention(run_root)  # nothing is refused as a second SPAWN


@pytest.mark.parametrize(
    "body",
    [[1, 2], {"artifact_format_version": 1, "job": "b"}],
    ids=["not an object", "an object missing the identity fields"],
)
def test_ss11a_an_index_entry_that_names_no_run_is_floored(tmp_path: Path, body: Any) -> None:
    """ss11a: an index entry whose body is not a run's identity still NAMES a
    run_id (its filename), and deleting it authorizes a spawn."""
    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _run_job(engine, "b")
    effect = next(e for e in _spawn_effects(run_root, 1) if e["job"] == "b")
    _tombstone(run_root, effect)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    _attest(run_root, 1)
    index = run_root / "runs" / ".by_run_id" / effect["run_id"]
    assert _by_path(plan_retention(run_root), index).verdict == "prunable"  # the twin
    durable_write(str(index), canonical_bytes(body) + b"\n")
    entry = _by_path(plan_retention(run_root), index)
    assert entry.verdict == "floored"
    assert "cannot read still names a run_id" in entry.why


# ------------------------------------------------------- the archive (DL-144)


def test_pr53_a_dry_run_archive_writes_no_receipt_and_lets_period_two_follow_one(
    tmp_path: Path,
) -> None:
    """PR-53: a dry run proves eligibility and writes nothing. Period 1 is
    counted as archived for the prefix condition of period 2, so one dry
    run reports both, as the real run would."""
    run_root = tmp_path / "run"
    _archivable(run_root)
    report = _archive(run_root, dry_run=True)
    assert report.refused == ()
    assert {item.period_id for item in report.removed if item.kind == "wal"} == {1, 2}
    assert not archive_receipt_path(run_root, 1).exists()
    assert wal_path(run_root, 1).exists() and wal_path(run_root, 2).exists()


def test_pr54_a_segment_gone_since_the_plan_refuses_and_names_the_replan(
    tmp_path: Path,
) -> None:
    """PR-54: the re-check reads the live disk. A WAL that vanished after
    planning, with no receipt licensing it, is loss for a re-plan to name
    and not something to receipt."""
    run_root = tmp_path / "run"
    _archivable(run_root)
    plan = plan_retention(run_root)
    wal_path(run_root, 1).unlink()
    report = prune(plan, classes=(ARCHIVE_CLASS,), dry_run=False)
    assert any("is gone since the plan was computed" in why for _, why in report.refused)
    assert not any(item.kind == "wal" and item.period_id == 1 for item in report.removed)
    assert not archive_receipt_path(run_root, 1).exists()  # and no receipt was written for it


def test_pr54_an_anchor_gone_since_the_plan_refuses_the_archive(tmp_path: Path) -> None:
    """PR-54: the re-check reads the LIVE anchor, never one the plan
    carried. A head that is gone refuses the receipt."""
    run_root = tmp_path / "run"
    _archivable(run_root)
    plan = plan_retention(run_root)
    (default_anchor_dir(run_root) / "anchor.json").unlink()
    assert EstateAnchor(plan.anchor_dir).read() is None
    report = prune(plan, classes=(ARCHIVE_CLASS,), dry_run=False)
    assert report.removed == ()
    assert any(
        "the lineage head is gone or names another estate" in why for _, why in report.refused
    )
    assert wal_path(run_root, 1).exists()


def test_pr54_a_spool_back_on_disk_since_the_plan_refuses_the_archive(tmp_path: Path) -> None:
    """PR-54, PR-36b's order: a tombstone that reappears between the plan
    and the receipt blocks the archive, and the refusal names it."""
    run_root = tmp_path / "run"
    engine = _open_period_one(run_root)
    _run_job(engine, "b")
    effect = next(e for e in _spawn_effects(run_root, 1) if e["job"] == "b")
    for boundary_no in range(1, 4):
        text = [C2_JIL, "insert_job: a\njob_type: c\ncommand: x\n"][(boundary_no - 1) % 2]
        asyncio.run(
            _seal(engine, _request(engine, _stage(run_root, text), request_id=f"r-{boundary_no}"))
        )
        _close(engine)
        engine = _resume(run_root, text)
    _close(engine)
    for period_id in (1, 2, 3):
        _attest(run_root, period_id)
    plan = plan_retention(run_root)
    assert _by_path(plan, wal_path(run_root, 1)).verdict == "prunable"  # no spool at plan time
    run_dir = _tombstone(run_root, effect)
    report = prune(plan, classes=(ARCHIVE_CLASS,), dry_run=False)
    assert any(
        "its spool is back on disk and must be pruned first" in why and str(run_dir) in why
        for _, why in report.refused
    )
    assert wal_path(run_root, 1).exists()


def test_ss11a_a_spawn_with_no_run_id_has_no_index_entry_to_prune(tmp_path: Path) -> None:
    """The tombstone paths one run owns are the directory, the default logs
    and, only when the run has a run_id, the index entry."""
    with_id = retention_mod._spool_paths(tmp_path, "j", 1, "7d9c2f4e-3b1a-4c5d-8e6f-0a1b2c3d4e5f")
    without = retention_mod._spool_paths(tmp_path, "j", 1, None)
    assert with_id[:-1] == without
    assert with_id[-1] == tmp_path / "runs" / ".by_run_id" / "7d9c2f4e-3b1a-4c5d-8e6f-0a1b2c3d4e5f"


def test_ss12_a_keep_and_an_age_threshold_leave_artifacts_with_no_run_untouched(
    tmp_path: Path,
) -> None:
    """`--keep-runs` and `--older-than-days` rank RUNS. An archive-class
    artifact belongs to no run, so both flags pass it through unchanged."""
    run_root = tmp_path / "run"
    _archivable(run_root)
    plan = plan_retention(run_root)
    everything = prune(plan, classes=(ARCHIVE_CLASS,), dry_run=True)
    filtered = prune(plan, classes=(ARCHIVE_CLASS,), dry_run=True, keep_runs=1, older_than_days=0.0)
    wals = {item.path for item in everything.removed if item.kind == "wal"}
    assert wals == {wal_path(run_root, 1), wal_path(run_root, 2)}
    assert {item.path for item in filtered.removed if item.kind == "wal"} == wals


def test_ss12_an_unreadable_mtime_reads_as_too_new_to_touch(tmp_path: Path) -> None:
    """The age filter feeds a deletion: a stat that fails is `inf`, newer
    than any cutoff, and never "older than anything"."""
    assert retention_mod._mtime(tmp_path / "gone") == float("inf")
    present = tmp_path / "present"
    present.write_text("x")
    assert retention_mod._mtime(present) == present.stat().st_mtime


def test_ss12_the_run_root_itself_is_never_removed(tmp_path: Path) -> None:
    run_root, _ = _attested_tombstone_root(tmp_path)
    plan = plan_retention(run_root)
    item = plan.prunable()[0]
    aimed = replace(item, path=plan.run_root)
    with pytest.raises(EngineError, match="is the run root itself"):
        retention_mod._remove(plan, aimed)
    assert run_root.is_dir() and isinstance(aimed, Artifact)


def test_ss12_an_unreadable_entry_counts_as_no_bytes_and_a_directory_sums_its_files(
    tmp_path: Path,
) -> None:
    """The reported size is a statistic of the sweep: a file that cannot be
    stat'ed (here a dangling link) adds nothing and does not abort it."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "one").write_bytes(b"12345")
    (tree / "two").write_bytes(b"123")
    os.symlink(tmp_path / "nowhere", tree / "dangling")
    assert retention_mod._size_of(tree) == 8
    assert retention_mod._file_size(tree / "dangling") == 0
    assert retention_mod._size_of(tree / "one") == 5
