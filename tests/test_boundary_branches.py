"""Branch tests for `dsl41.boundary` that the boundary, estate and nightbank
suites do not reach (S6, group c).

Normative spec: `docs/period-model.md` ss1.3 (the anchor and its
transitions), ss2.2 (the `seal` record), ss3.5 (executions), ss6-ss7 (the
boundary's phases), ss11 (resume) and DL-224 (whose root this is). The
anchor is driven through its own transitions over hand-built heads; the
phase checks are driven over the real contexts the engine builds. Each
refusal has a twin that does not trigger it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_boundary import (
    _genesis,
    _close,
    _staged_context,
    _start_a,
)
from test_retention import _periods
from test_seal_artifact import (
    T,
    _closing,
    _manifest_of,
    _seal,
    _staged,
)

import dsl41.boundary as boundary
from dsl41.boundary import (
    Anchor,
    BoundaryContext,
    Candidate,
    Claim,
    ClaimedHead,
    ClosedHead,
    EstateAnchor,
    Lineage,
    OpenHead,
    PeriodRow,
    Reclaimed,
    RootAuthorityError,
    act_on_head,
    check_candidate,
    check_seal_record,
    executions_at,
    filesystem_type,
    live_spawns,
    newest_opened_period,
    no_crash,
    pending_reclaim,
    read_candidate,
    read_seal,
    require_resume_root,
    resume_root_refusal,
    select_seal,
    seal_record,
    validate_boundary,
    validate_staged,
)
from dsl41.canon import canonical_bytes
from dsl41.ir import CatalogIR
from dsl41.oracle_state import JobRuntime
from dsl41.period import RuntimeProfile, period_dir, wal_path
from dsl41.runner_clock import EngineError
from dsl41.runner_effects import Effect, EffectOutcome, Outbox

ESTATE = "estate-s6c"
SEAL_A = "sha256:" + "a" * 64
SEAL_B = "sha256:" + "b" * 64
RUN_1 = "7d9c2f4e-3b1a-4c5d-8e6f-0a1b2c3d4e5f"


# --------------------------------------------------- filesystem_type (ss1.3)


def test_pr04_a_linux_mount_table_names_the_longest_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-04: where `/proc/mounts` exists the longest mount point that
    prefixes the real path wins, so a nested mount is resolved. A line
    with fewer than three fields is not a mount. The table is injected,
    so the rule holds on every platform the suite runs on."""
    table = "/dev/sda1 / ext4 rw 0 0\nserver:/export /mnt/nfs nfs4 rw 0 0\nshort line\n"
    real = Path.read_text

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if str(self) == "/proc/mounts":
            return table
        return real(self, *args, **kwargs)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", read_text)
    assert filesystem_type(Path("/mnt/nfs/anchor")) == "nfs4"
    assert filesystem_type(Path("/srv/anchor")) == "ext4"


def test_pr04_a_mount_table_that_cannot_be_read_is_an_unknown_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-04: an undetectable filesystem is None, and refusing every unknown
    would make the anchor unusable on a platform this cannot read."""

    def unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        raise OSError("EACCES: /proc/mounts")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", unreadable)
    assert filesystem_type(Path("/srv/anchor")) is None


def test_pr04_mount_8_output_is_parsed_and_noise_lines_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-04, the non-Linux route: `<device> on <point> (<type>, ...)`. A
    line without those markers is skipped, and a missing or hung `mount`
    is an unknown."""
    output = "/dev/disk1 on / (apfs, local)\nnot a mount line\n//srv/share on /Volumes/share (smbfs, nodev)\n"

    def run(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", run)
    assert filesystem_type(Path("/Volumes/share/anchor")) == "smbfs"
    assert filesystem_type(Path("/Users/anchor")) == "apfs"

    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("/sbin/mount")

    def hung(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("mount", 10)

    for failure in (missing, hung):
        monkeypatch.setattr(subprocess, "run", failure)
        assert filesystem_type(Path("/Users/anchor")) is None


# ------------------------------------------------------ the anchor (ss1.3)


@pytest.fixture
def anchor(tmp_path: Path):
    handle = EstateAnchor(tmp_path / "anchor")
    handle.acquire()
    yield handle
    handle.release()


def _closed(anchor: EstateAnchor, root: Path, digest: str = SEAL_A) -> Anchor:
    """Period 1 opened, finalized and closed at `digest`."""
    anchor.create_open(estate_id=ESTATE, root=root)
    anchor.finalize(1)
    return anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=digest)


def _claimed(anchor: EstateAnchor, root: Path, *, next_period: int = 2) -> Claim:
    _closed(anchor, root)
    return anchor.claim_successor(
        estate_id=ESTATE, seal_digest=SEAL_A, next_period=next_period, target_root=root
    )


def test_ss1_3_an_anchor_that_is_not_a_json_object_refuses_and_an_absent_one_is_none(
    anchor: EstateAnchor,
) -> None:
    assert anchor.read() is None
    anchor.path.write_bytes(canonical_bytes([1, 2]) + b"\n")
    with pytest.raises(EngineError, match="not a JSON object"):
        anchor.read()


def test_ss1_3_a_claim_that_is_not_a_json_object_refuses_and_an_absent_one_is_none(
    anchor: EstateAnchor,
) -> None:
    claim_id = "sha256:" + "c" * 64
    assert anchor.read_claim(claim_id) is None
    path = anchor.claim_path(claim_id)
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_bytes([1, 2]) + b"\n")
    with pytest.raises(EngineError, match="not a JSON object"):
        anchor.read_claim(claim_id)


def test_ss1_3_an_attestation_needs_a_registry_row_and_the_root_that_holds_it(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    _closed(anchor, root)
    with pytest.raises(EngineError, match="period 9 has no registry row to attest"):
        anchor.attest(9, estate_id=ESTATE, root=root, seal_digest=SEAL_A)
    with pytest.raises(EngineError, match="lives in .*not .*elsewhere"):
        anchor.attest(1, estate_id=ESTATE, root=tmp_path / "elsewhere", seal_digest=SEAL_A)
    done = anchor.attest(1, estate_id=ESTATE, root=root, seal_digest=SEAL_A)  # the twin
    assert done.row(1).attested is True  # type: ignore[union-attr]


def test_ss1_3_a_reclaim_goes_back_to_the_closed_head_the_claim_names(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    """The twin of the refusals below: a sound claim is moved back."""
    root = tmp_path / "run"
    root.mkdir()
    claim = _claimed(anchor, root)
    updated, moved = anchor.reclaim(estate_id=ESTATE, claimed_actor="op@host")
    assert updated.head == ClosedHead(
        period_id=1, seal_digest=SEAL_A, closing_root=str(root.resolve())
    )
    assert moved.claim_id == claim.claim_id and moved.claimed_actor == "op@host"
    assert pending_reclaim(updated, 2) == moved


def test_ss1_3_a_reclaim_over_a_claim_file_that_is_gone_refuses(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    claim = _claimed(anchor, root)
    anchor.claim_path(claim.claim_id).unlink()
    with pytest.raises(EngineError, match="is not there"):
        anchor.reclaim(estate_id=ESTATE, claimed_actor="op@host")


def test_ss1_3_a_reclaim_refuses_a_strangers_claim(anchor: EstateAnchor, tmp_path: Path) -> None:
    """The claim id binds `{prev_seal_digest, next_period, target_root}` and
    not the estate, so a claim body of another estate under the head's
    filename recomputes to the same id and is caught by the estate check."""
    root = tmp_path / "run"
    root.mkdir()
    claim = _claimed(anchor, root)
    anchor.write_claim(Claim(**{**claim.model_dump(), "estate_id": "another-estate"}))
    with pytest.raises(EngineError, match="a stranger's claim"):
        anchor.reclaim(estate_id=ESTATE, claimed_actor="op@host")


def test_ss1_3_a_reclaim_refuses_a_claim_and_head_that_name_different_roots(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    claim = _claimed(anchor, root)
    stored = anchor.read()
    assert stored is not None
    anchor.write(
        stored.model_copy(
            update={"head": ClaimedHead(claim_id=claim.claim_id, target_root=str(tmp_path / "x"))}
        )
    )
    with pytest.raises(EngineError, match="the claim and the head disagree"):
        anchor.reclaim(estate_id=ESTATE, claimed_actor="op@host")


def test_ss1_3_a_reclaim_needs_a_registry_row_for_the_closing_period(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    """A claim for period 5 reclaims to a `closed` period 4, which this
    anchor's registry never held."""
    root = tmp_path / "run"
    root.mkdir()
    _claimed(anchor, root, next_period=5)
    with pytest.raises(EngineError, match="period 4 has no registry row"):
        anchor.reclaim(estate_id=ESTATE, claimed_actor="op@host")


def test_ss1_3_finalize_needs_the_registry_row_and_is_idempotent(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    anchor.write(Anchor(estate_id=ESTATE, head=OpenHead(period_id=1, root=str(root))))
    with pytest.raises(EngineError, match="period 1 has no registry row to finalize"):
        anchor.finalize(1)
    anchor.write(
        Anchor(
            estate_id=ESTATE,
            head=OpenHead(period_id=1, root=str(root)),
            periods={"1": PeriodRow(root=str(root))},
        )
    )
    first = anchor.finalize(1)
    assert first.row(1).segment_durable is True  # type: ignore[union-attr]
    assert anchor.finalize(1) == first  # a re-run writes nothing


def test_ss1_3_a_period_closed_at_one_seal_is_not_closed_at_another(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    closed = _closed(anchor, root, SEAL_A)
    assert (
        anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A) == closed
    )
    with pytest.raises(
        EngineError, match=rf"already closed at\s+{SEAL_A}.*this seal says {SEAL_B}"
    ):
        anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_B)


def test_ss1_3_a_claimed_head_is_not_closed_and_is_spelled_as_a_claim(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    claim = _claimed(anchor, root)
    with pytest.raises(
        EngineError, match=rf"cannot close period 2: the head is\s+claimed\({claim.claim_id}"
    ):
        anchor.close_period(estate_id=ESTATE, period_id=2, root=root, seal_digest=SEAL_B)


def test_ss1_3_a_claim_whose_file_was_lost_is_rewritten_by_its_own_retry(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    """ss1.3: idempotent on `claim_id`. A retry with the claim file present
    answers with it; with the file gone the same claim is written again."""
    root = tmp_path / "run"
    root.mkdir()
    claim = _claimed(anchor, root)
    again = anchor.claim_successor(
        estate_id=ESTATE, seal_digest=SEAL_A, next_period=2, target_root=root
    )
    assert again == claim  # the twin: the stored claim is returned as it was
    anchor.claim_path(claim.claim_id).unlink()
    rewritten = anchor.claim_successor(
        estate_id=ESTATE, seal_digest=SEAL_A, next_period=2, target_root=root
    )
    assert rewritten.claim_id == claim.claim_id
    assert anchor.read_claim(claim.claim_id) == rewritten


def test_ss1_3_opening_under_a_claim_is_idempotent_and_refuses_another_head(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    claim = _claimed(anchor, root)
    with pytest.raises(EngineError, match="cannot open period 2 under claim sha256:dead"):
        anchor.open_claimed(claim_id="sha256:dead", period_id=2, root=root)
    opened = anchor.open_claimed(claim_id=claim.claim_id, period_id=2, root=root)
    assert isinstance(opened.head, OpenHead) and opened.head.period_id == 2
    assert anchor.open_claimed(claim_id=claim.claim_id, period_id=2, root=root) == opened
    with pytest.raises(EngineError, match=r"cannot open period 3 .*the head is\s+open\(period 2"):
        anchor.open_claimed(claim_id=claim.claim_id, period_id=3, root=root)


def test_ss1_3_a_reclaim_is_found_by_its_period_newest_first() -> None:
    older = Reclaimed(
        claim_id="c2",
        target_root="/r",
        next_period=2,
        claimed_actor="a",
        at="2026-01-01T00:00:00.000000",
    )
    newer = Reclaimed(
        claim_id="c3",
        target_root="/r",
        next_period=3,
        claimed_actor="b",
        at="2026-01-02T00:00:00.000000",
    )
    anchor = Anchor(
        estate_id=ESTATE,
        head=OpenHead(period_id=1, root="/r"),
        reclaimed=[older, newer],
    )
    assert pending_reclaim(anchor, 2) == older  # skips the newer entry for another period
    assert pending_reclaim(anchor, 3) == newer
    assert pending_reclaim(anchor, 9) is None


# ------------------------------------------------------- the artifact reader


def test_ss7_a_candidate_that_is_not_a_json_object_refuses(tmp_path: Path) -> None:
    assert read_candidate(tmp_path) is None
    (tmp_path / "candidate.json").write_bytes(canonical_bytes([1]) + b"\n")
    with pytest.raises(EngineError, match="not a JSON object"):
        read_candidate(tmp_path)


# ---------------------------------------------- ss6-ss7 phase checks, by rule


def test_ss7_phase_two_refuses_a_carried_state_that_is_not_at_the_cutoff(tmp_path: Path) -> None:
    """ss6: C1 owns every tick <= T, so the carried state's clock is T."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _staged_context(run_root, engine)
    manifest = SimpleNamespace(runtime_profile=RuntimeProfile())
    at_cutoff = BoundaryContext(
        staged=staged,
        committed=None,  # type: ignore[arg-type]
        committed_manifest=manifest,  # type: ignore[arg-type]
        at=staged.carried_state.now,
        post_barrier_state=staged.carried_state,
    )
    assert validate_boundary(at_cutoff).refused == ()  # the twin
    late = replace(at_cutoff, at=staged.carried_state.now + timedelta(seconds=1))
    with pytest.raises(EngineError, match="the carried state is not at the cutoff"):
        validate_boundary(late)
    _close(engine)


def test_pr28a_phase_two_refuses_work_the_barrier_made_live_under_a_changed_closure(
    tmp_path: Path,
) -> None:
    """PR-28a: the classifier runs AGAIN after the barrier, over the
    post-barrier state. Phase 1 saw `a` idle, so a C2 that changes `a` passes
    readiness; the cutoff's own admission then starts `a`, and only phase 2's
    re-classification can refuse it. Phase 1's stale state, classified again,
    would not."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    changed = (
        "insert_job: a\njob_type: c\ncommand: DIFFERENT\n\ninsert_job: b\njob_type: c\ncommand: y\n"
    )
    staged = _staged_context(run_root, engine, text=changed)
    assert validate_staged(staged).refused == ()  # phase 1: `a` is idle, nothing live to refuse
    _start_a(engine)  # the barrier's admission: `a` is now RUNNING under C1
    post_barrier = engine._carried(staged.at)
    assert post_barrier.now == staged.carried_state.now
    context = BoundaryContext(
        staged=staged,
        committed=None,  # type: ignore[arg-type]
        committed_manifest=SimpleNamespace(runtime_profile=RuntimeProfile()),  # type: ignore[arg-type]
        at=post_barrier.now,
        post_barrier_state=post_barrier,
    )
    with pytest.raises(
        EngineError, match=r"the post-barrier classification refuses the boundary \(a\)"
    ):
        validate_boundary(context)
    # the twin: the same context over the stale phase-1 state is not refused
    stale = replace(context, post_barrier_state=staged.carried_state)
    assert validate_boundary(stale).refused == ()
    _close(engine)


def _candidate_context(**overrides: Any) -> tuple[BoundaryContext, Any, dict[str, Any]]:
    """The golden seal as the candidate, with the context phase 2 holds."""
    sidecar = _seal()
    context = BoundaryContext(
        staged=None,  # type: ignore[arg-type]
        committed=sidecar.next_period,
        committed_manifest=_manifest_of(json.loads(sidecar.to_bytes())),
        at=T,
        post_barrier_state=None,  # type: ignore[arg-type]
    )
    return replace(context, **overrides), sidecar, seal_record(sidecar)


def test_ss7_a_candidate_that_agrees_with_its_cutoff_passes_phase_two() -> None:
    context, sidecar, record = _candidate_context()
    check_candidate(context, sidecar=sidecar, record=record)


def test_ss3_4_a_candidate_whose_first_index_skips_the_cutoff_refuses() -> None:
    """PR-05b, I2: the opening's first index is `closes_at_index + 1`."""
    context, sidecar, record = _candidate_context()
    skipped = replace(context, committed=context.committed.model_copy(update={"first_index": 5312}))
    with pytest.raises(
        EngineError, match=r"first_index 5312 is not\s+closes_at_index \+ 1 \(5311\)"
    ):
        check_candidate(skipped, sidecar=sidecar, record=record)


def test_ss2_2_a_seal_record_that_disagrees_with_its_sidecar_refuses() -> None:
    context, sidecar, record = _candidate_context()
    record["closes_at_index"] = 1
    with pytest.raises(EngineError, match=r"disagrees with the sidecar it names \(closes_at_index"):
        check_candidate(context, sidecar=sidecar, record=record)


def test_ss6_a_snapshot_that_is_not_at_the_cutoff_refuses() -> None:
    """ss6: C1 owns every tick <= T and C2 every tick after it."""
    context, sidecar, record = _candidate_context()
    late = replace(context, at=T + timedelta(microseconds=1))
    with pytest.raises(EngineError, match="the snapshot is not at the cutoff"):
        check_candidate(late, sidecar=sidecar, record=record)


def test_ss2_2_a_record_that_is_not_a_seal_is_refused_by_kind() -> None:
    with pytest.raises(EngineError, match="not a seal record: rec is 'segment'"):
        check_seal_record({"rec": "segment"})
    check_seal_record(seal_record(_seal()))  # the twin


# ----------------------------------------------- the executions (ss3.5, ss8)


def _spawn(job: str, *, run_id: str | None = RUN_1, index: int = 5, number: int = 1) -> Effect:
    return Effect(
        effect_id=f"e{index}:SPAWN:{job}.{number}",
        kind="SPAWN",
        job=job,
        run_number=number,
        executor_id="local",
        index=index,
        at=T,
        run_id=run_id,
        generation=0,
    )


def _running(*jobs: str) -> dict[str, JobRuntime]:
    return {job: JobRuntime(status="RUNNING", status_at=T, run_number=1) for job in jobs}


def test_ss3_5_only_pending_and_applied_spawns_have_a_live_run_behind_them() -> None:
    """A retired or indeterminate SPAWN is not an execution to carry, even
    when the row it was born under is still live."""
    outbox = Outbox()
    states = {"pend": None, "app": "applied", "ret": "retired", "ind": "indeterminate"}
    for index, (job, state) in enumerate(states.items(), start=1):
        effect = _spawn(job, run_id=f"7d9c2f4e-3b1a-4c5d-8e6f-0a1b2c3d4e{index:02x}", index=index)
        outbox.record(effect)
        if state is not None:
            outbox.resolve(EffectOutcome(effect_id=effect.effect_id, state=state))  # type: ignore[arg-type]
    seen = [(effect.job, state) for effect, state in live_spawns(outbox, _running(*states))]
    assert seen == [("pend", "pending"), ("app", "applied")]


def test_pr36a_a_pending_spawn_with_no_run_id_cannot_be_carried(tmp_path: Path) -> None:
    """ss11a: every SPAWN a seal carries binds a run_id, and one written
    before that rule is refused by name rather than carried unbound."""
    outbox = Outbox()
    outbox.record(_spawn("j", run_id=None))
    with pytest.raises(EngineError, match="a SPAWN with no run_id"):
        executions_at(
            run_root=tmp_path,
            outbox=outbox,
            rows=_running("j"),
            catalog=CatalogIR(),
            interval_default=60,
        )
    twin = Outbox()
    twin.record(_spawn("j"))
    [entry] = executions_at(
        run_root=tmp_path, outbox=twin, rows=_running("j"), catalog=CatalogIR(), interval_default=60
    )
    assert (entry.kind, entry.run_id) == ("pending_spawn", RUN_1)


def test_ss11_a_watch_log_with_no_claimed_entry_in_the_seal_being_rederived_refuses(
    tmp_path: Path,
) -> None:
    """ss11: audit re-derives a seal's executions with the per-run count the
    seal claims. A run that has a `watch.jsonl` and no claimed entry is the
    evidence and the claim disagreeing, and refuses rather than folding a
    stranger's progress."""
    outbox = Outbox()
    effect = _spawn("j")
    outbox.record(effect)
    outbox.resolve(EffectOutcome(effect_id=effect.effect_id, state="applied", run_id=RUN_1))
    run_dir = tmp_path / "runs" / "j.1"
    run_dir.mkdir(parents=True)
    (run_dir / "watch.jsonl").write_text("")
    with pytest.raises(EngineError, match="carries no fw_watch entry for this run"):
        executions_at(
            run_root=tmp_path,
            outbox=outbox,
            rows=_running("j"),
            catalog=CatalogIR(),
            interval_default=60,
            watch_prefix={},
        )


# ------------------------------------------------------------ the install


def test_ss7_the_boundary_will_not_reuse_a_period_directory_it_cannot_identify(
    tmp_path: Path,
) -> None:
    """ss7, PR-30d: a directory at the successor's path with no
    `candidate.json` is not blindly reused. With nothing there, the
    refusal is the other one: the request names bytes never staged."""
    committed = _closing()
    with pytest.raises(EngineError, match="no staged candidate at this digest"):
        boundary._prepare_install(
            tmp_path, staged=_staged(), committed_manifest=committed, crash_point=no_crash
        )
    period_dir(tmp_path, committed.period_id).mkdir(parents=True)
    with pytest.raises(EngineError, match="exists and carries no candidate.json"):
        boundary._prepare_install(
            tmp_path, staged=_staged(), committed_manifest=committed, crash_point=no_crash
        )


def test_pr30d_quarantining_the_same_bytes_twice_is_idempotent(tmp_path: Path) -> None:
    """PR-30d: the destination is named by the candidate's digest and the
    manifest's, so the same bytes quarantined again land on a destination
    that exists, and the superseded directory is removed instead."""
    installed = Candidate(stage_digest="sha256:" + "d" * 64, next_period=_staged())
    target = period_dir(tmp_path, 3)

    def superseded() -> None:
        target.mkdir(parents=True)
        (target / "manifest.json").write_bytes(b'{"same":"bytes"}\n')

    superseded()
    boundary._quarantine(tmp_path, target, installed)
    [kept] = list((tmp_path / "periods" / ".quarantine").glob("*/*"))
    assert not target.exists() and (kept / "manifest.json").read_bytes() == b'{"same":"bytes"}\n'
    superseded()
    boundary._quarantine(tmp_path, target, installed)
    assert not target.exists()
    assert list((tmp_path / "periods" / ".quarantine").glob("*/*")) == [kept]


# ------------------------------------------------------ resume (ss11, ss1.3)


def test_ss3_4_a_segment_an_interrupted_opener_wrote_must_agree_with_the_seal(
    tmp_path: Path,
) -> None:
    """ss3.4: an existing segment is verified, never appended to. The link
    and the `reclaimed` stamp are held to the boundary's, as the pins are."""
    run_root = tmp_path / "run"
    _periods(run_root, 2)
    seal = read_seal(run_root, 1)
    segment = wal_path(run_root, 2)
    link = {"period_id": 1, "digest": seal.digest}
    boundary._check_existing_segment(segment, seal.next_period, link)  # the twin
    with pytest.raises(EngineError, match=r"opens_from_seal: segment"):
        boundary._check_existing_segment(
            segment, seal.next_period, {"period_id": 1, "digest": SEAL_B}
        )
    forced = Reclaimed(
        claim_id="c",
        target_root="/r",
        next_period=2,
        claimed_actor="op",
        at="2026-01-01T00:00:00.000000",
    )
    with pytest.raises(EngineError, match=r"reclaimed: segment None vs"):
        boundary._check_existing_segment(segment, seal.next_period, link, forced)


def test_ss11_a_sidecar_that_exists_and_cannot_be_read_is_unreadable_not_missing(
    tmp_path: Path,
) -> None:
    (tmp_path / "seals" / "000001.json").mkdir(parents=True)
    with pytest.raises(EngineError, match=r"000001.json: unreadable"):
        read_seal(tmp_path, 1)
    with pytest.raises(EngineError, match="names a sidecar that is not there"):
        read_seal(tmp_path, 2)


def test_ss11_the_segment_that_opened_from_a_seal_names_its_digest(tmp_path: Path) -> None:
    """ss11 step 3: a rolled root selects by the segment's own link, and a
    sidecar whose digest is not the one the link names is an orphan."""
    run_root = tmp_path / "run"
    _periods(run_root, 2)
    seal = read_seal(run_root, 1)
    opening = {"opens_from_seal": {"period_id": 1, "digest": seal.digest}}
    assert select_seal(run_root, [opening]) == Lineage(seal=seal, opens_next=False)
    stranger = {"opens_from_seal": {"period_id": 1, "digest": SEAL_B}}
    with pytest.raises(EngineError, match="an orphan or a stranger's sidecar"):
        select_seal(run_root, [stranger])


def test_ss1_3_a_claimed_head_whose_claim_file_is_gone_refuses_at_resume(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    claim = _claimed(anchor, root)
    lineage = Lineage(seal=None, opens_next=False)
    assert act_on_head(anchor, run_root=root, estate_id=ESTATE, lineage=lineage).head == (
        ClaimedHead(claim_id=claim.claim_id, target_root=str(root.resolve()))
    )  # the twin: with its claim the head is left to the opener
    anchor.claim_path(claim.claim_id).unlink()
    with pytest.raises(EngineError, match="is not there"):
        act_on_head(anchor, run_root=root, estate_id=ESTATE, lineage=lineage)


def _write_wal(root: Path, number: int, first: bytes) -> None:
    (root / "wal").mkdir(exist_ok=True)
    (root / "wal" / f"{number:06d}.jsonl").write_bytes(first)


def test_dl224_the_newest_opened_period_is_read_from_the_opening_record(tmp_path: Path) -> None:
    assert newest_opened_period(tmp_path) is None  # no segment at all
    _write_wal(tmp_path, 1, b'{"rec":"segment","period_id":1}\n')
    assert newest_opened_period(tmp_path) == 1
    # a torn newest segment never opened: the one before it answers
    _write_wal(tmp_path, 2, b'{"rec":"segm')
    assert newest_opened_period(tmp_path) == 1
    # a newest segment with no newline that IS a document did open
    _write_wal(tmp_path, 2, b'{"rec":"segment","period_id":2}')
    assert newest_opened_period(tmp_path) == 2


@pytest.mark.parametrize("first", [b"not json\n", b'{"rec":"seal"}\n', b"[1]\n"])
def test_dl224_a_first_line_that_is_not_an_opening_names_no_period(
    tmp_path: Path, first: bytes
) -> None:
    """`read_journal` is the reader that refuses such a root, in its own
    words; this one reports none rather than guessing."""
    _write_wal(tmp_path, 1, first)
    assert newest_opened_period(tmp_path) is None


def test_dl224_a_segment_that_cannot_be_opened_is_unreadable(tmp_path: Path) -> None:
    (tmp_path / "wal" / "000001.jsonl").mkdir(parents=True)
    with pytest.raises(EngineError, match=r"000001.jsonl: unreadable"):
        newest_opened_period(tmp_path)


def test_dl224_a_root_with_no_opened_segment_has_no_period_to_own(tmp_path: Path) -> None:
    """ss1.3: NAMED is the early filter and OWNED needs a period. A root the
    anchor names with no segment yet is accepted; one it does not name is
    refused by name."""
    root = tmp_path / "run"
    root.mkdir()
    named = Anchor(
        estate_id=ESTATE,
        head=OpenHead(period_id=1, root=str(root.resolve())),
        periods={"1": PeriodRow(root=str(root.resolve()))},
    )
    require_resume_root(named, anchor_path=tmp_path / "anchor.json", run_root=root)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(RootAuthorityError, match="this anchor does not name"):
        require_resume_root(named, anchor_path=tmp_path / "anchor.json", run_root=other)


def test_dl224_a_locked_pre_check_of_a_root_with_no_sentinel_has_no_refusal(
    tmp_path: Path,
) -> None:
    """`resume_run` takes its own no-sentinel path, so the pre-check says
    nothing about such a root."""
    assert resume_root_refusal(tmp_path / "run", tmp_path / "anchor", locked=True) is None
