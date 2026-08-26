"""Boundary tests: the anchor, the genesis transaction, the cutoff barrier
and the seal operation (period-model ss1.1, ss1.3, ss6-ss9, ss11; DL-133).

Obligations in ss13 exercised here: PR-01a/b/c, PR-02, PR-02b, PR-03,
PR-04, PR-05b, PR-07's operational half, PR-17, PR-25, PR-25a, PR-26,
PR-27, PR-28, PR-28a, PR-28b, PR-28e, PR-29, PR-30, PR-30c, PR-30d,
PR-30f, PR-30g, PR-32, PR-33, PR-34's barrier half and PR-45's in-place
rows.

House style follows test_seal_artifact.py and test_fw_spool.py: the crash
matrix drives the operation's OWN seam (`Engine.crash_point`) rather than
killing a process and hoping it died in the window it meant, and every
refusal case asserts the message fragment that only its own rule
produces.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from dsl41.ast_jil import parse, render_preserve
from dsl41.boundary import (
    Anchor,
    ClaimedHead,
    ClosedHead,
    CommittedBoundary,
    EstateAnchor,
    Lineage,
    OpenHead,
    PeriodSealed,
    SealRequest,
    claim_id_for,
    claim_root,
    default_anchor_dir,
    executing_jobs,
    filesystem_type,
    open_next_period,
    read_candidate,
    read_seal,
    retry_horizon_gate,
    select_seal,
    stage_next_period,
)
from dsl41.ir import CatalogIR, lower_catalog
from dsl41.oracle_state import Event
from dsl41.attest import Attestation, audit_period
from dsl41.period import (
    RETRY_HORIZON_S,
    RuntimeProfile,
    SEGMENT_FIELDS,
    SourceFile,
    active_wal,
    period_dir,
    read_period_manifest,
    read_sentinel,
    seal_path,
    stage_manifest,
    staging_dir,
    wal_path,
    wal_segments,
    write_bundle,
)
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter, SupervisorUnavailable
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_journal import read_journal
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import resume_run, start_run
from dsl41.seal import StagedNextPeriod

T0 = datetime(2026, 7, 1, 8, 0)

C1_JIL = "insert_job: a\njob_type: c\ncommand: x\n\ninsert_job: b\njob_type: c\ncommand: y\n"
#: C2 touches `b` only -- `a` is what the tests keep live across the boundary
C2_JIL = "insert_job: a\njob_type: c\ncommand: x\n\ninsert_job: b\njob_type: c\ncommand: CHANGED\n"


# ------------------------------------------------------------- fixtures


def _catalog(text: str, *, name: str = "estate.jil") -> tuple[CatalogIR, list[SourceFile]]:
    parsed = [parse(text, file=name)]
    return lower_catalog(parsed), [SourceFile(path=name, text=render_preserve(parsed[0]))]


#: the ss8 mode table requires the FULL drain of a tethered estate, so a
#: test that seals a live bound run pins the detached mode the rule allows
#: (FakeAdapter is neither, and inherits whatever the staged profile says)
DETACHED = RuntimeProfile(execution_mode="detached")


def _stage(
    run_root: Path,
    text: str,
    *,
    name: str = "estate.jil",
    profile: RuntimeProfile | None = None,
) -> StagedNextPeriod:
    """The CLI's staging half: the immutable bundle, then the two staged
    files under `periods/.staging/<stage_digest>/`."""
    catalog, sources = _catalog(text, name=name)
    manifest = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=profile or RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    return stage_next_period(run_root, staged_manifest=manifest)


def _genesis(
    run_root: Path,
    *,
    text: str = C1_JIL,
    clock: VirtualClock | None = None,
    profile: RuntimeProfile | None = None,
):
    """A periodized root with period 1 open, staged exactly as `dsl41 run`
    stages it."""
    catalog, sources = _catalog(text)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=profile or RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    return start_run(
        catalog,
        run_root,
        clock=clock or VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        staged=staged,
    )


def _request(engine, staged: StagedNextPeriod, **overrides: Any) -> SealRequest:
    fields: dict[str, Any] = {
        "baseline_id": engine.baseline_id,
        "epoch": engine.epoch,
        "request_id": "r-seal-1",
        "next_period": staged,
        "stage_digest": staged.stage_digest,
        "force_seal": False,
        "claimed_actor": "alice@ops",
    }
    return SealRequest(**{**fields, **overrides})


async def _seal(engine, request: SealRequest):
    """Drive the loop until the boundary answers. Returns the committed
    boundary, or raises whatever refused it.

    The horizon is `now` on purpose: the boundary check is the FIRST thing
    each iteration does, so a seal never needs the loop to reach forward --
    and a far horizon would make a scheduled estate compute occurrences out
    to the end of representable time."""
    future = engine.submit_seal(request)
    with pytest.raises(PeriodSealed) as sealed:
        await engine.run_until_quiescent(engine.clock.now())
    assert future.done()
    return sealed.value.boundary


async def _refused(engine, request: SealRequest) -> str:
    """Drive the loop over a boundary that must NOT commit, and hand back
    the refusal. The loop keeps running afterwards, which is the property
    `abort_boundary` exists for."""
    future = engine.submit_seal(request)
    await engine.run_until_quiescent(T0)
    assert future.done()
    with pytest.raises(EngineError) as refused:
        future.result()
    return str(refused.value)


def _close(engine) -> None:
    if engine.journal is not None:
        engine.journal.close()


# --------------------------------------------------- ss1.1 the sentinel


def test_pr01a_genesis_writes_the_sentinel_before_the_wal(tmp_path: Path) -> None:
    """ss1.1's ordered transaction, from the outside: the sentinel names
    the estate, `see` points at `wal/`, and the records are in
    `wal/000001.jsonl` rather than in the file an old binary would append
    to."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    _close(engine)
    sentinel = read_sentinel(run_root)
    assert sentinel is not None
    assert sentinel.rec == "period_root" and sentinel.see == "wal/"
    assert sentinel.claim_id is None
    # one line, and it is NOT the WAL
    assert len((run_root / "journal.jsonl").read_text().splitlines()) == 1
    assert wal_path(run_root, 1).exists()
    assert [r["rec"] for r in read_journal(wal_path(run_root, 1))][:2] == ["segment", "leader"]
    # every reader reaches the same records from the root, the sentinel or
    # the segment: the sentinel's `see`, followed once
    assert read_journal(run_root) == read_journal(run_root / "journal.jsonl")
    assert read_journal(run_root) == read_journal(wal_path(run_root, 1))


def test_pr01a_the_estate_id_is_read_back_never_minted_twice(tmp_path: Path) -> None:
    """PR-01a: killed after the sentinel and before the segment, a re-run of
    genesis completes idempotently and reads `estate_id` BACK."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    first = claim_root(run_root)
    assert not first.resumed
    again = claim_root(run_root)
    assert again.resumed and again.estate_id == first.estate_id
    # and the anchor genesis over that sentinel carries the same identity
    engine = _genesis(run_root)
    _close(engine)
    assert read_sentinel(run_root).estate_id == first.estate_id  # type: ignore[union-attr]
    anchor = EstateAnchor(default_anchor_dir(run_root))
    stored = anchor.read()
    assert stored is not None and stored.estate_id == first.estate_id


def test_pr01c_another_estates_sentinel_refuses_the_root(tmp_path: Path) -> None:
    """ss1.1's ownership rule: absent creates, our own incomplete
    transaction resumes, ANYTHING ELSE refuses -- another estate's, an
    earlier period of this estate's, or a concurrent opener's."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    claim_root(run_root, estate_id="estate-one")
    with pytest.raises(EngineError, match="is not ours"):
        claim_root(run_root, estate_id="estate-two")
    # and a claim that is not the one that created the root
    with pytest.raises(EngineError, match="is not ours"):
        claim_root(run_root, estate_id="estate-one", claim_id="sha256:" + "0" * 64)


def test_pr01c_a_retired_journal_refuses_and_names_the_dialect(tmp_path: Path) -> None:
    """D5's owner-local tombstone (DL-138). `claim_root` does not route
    through `read_journal`, so it carries its own registry: a `journal.jsonl`
    opening with a retired `header` refuses BY NAME, and anything else at
    that path is unknown residue and keeps the generic refusal.

    The tombstone is HEADER-ONLY. `result` and `effect` are retired RECORDS
    and were never legal openings, so a file that opens with one is not a
    pre-period root this build recognises -- it is residue like the garbage
    below, and naming DL-138 over it would tell an operator their root
    predates a retirement it never took part in (DL-138, the L2 review)."""
    retired = tmp_path / "retired"
    retired.mkdir()
    (retired / "journal.jsonl").write_text(json.dumps({"rec": "header"}) + "\n")
    with pytest.raises(EngineError, match="RETIRED") as named:
        claim_root(retired)
    assert "DL-138" in str(named.value)

    for kind in ("result", "effect"):
        never = tmp_path / f"never-{kind}"
        never.mkdir()
        (never / "journal.jsonl").write_text(json.dumps({"rec": kind}) + "\n")
        with pytest.raises(EngineError, match="already exists") as residue:
            claim_root(never)
        assert "DL-138" not in str(residue.value) and "RETIRED" not in str(residue.value)

    garbage = tmp_path / "garbage"
    garbage.mkdir()
    (garbage / "journal.jsonl").write_text("not a record at all\n")
    with pytest.raises(EngineError, match="already exists") as generic:
        claim_root(garbage)
    assert "DL-138" not in str(generic.value)


def test_pr08d_a_sentinel_this_binary_cannot_read_refuses_by_name(tmp_path: Path) -> None:
    """PR-08d, and `read_sentinel`'s own rule: absence is a fact and
    corruption is not.

    A file that EXISTS and holds a `period_root` record this binary cannot
    read -- an `artifact_format_version` it does not implement -- was
    swallowed with every other ss3.2 ingress refusal and returned as "no
    sentinel". The root still refused, at whatever gate met it next, but
    namelessly: nothing printed the version (DL-151).

    A first line this cannot READ AS a `period_root` record at all keeps the
    old answer -- not JSON, a different `rec`, or a line torn mid-write --
    because it is not a sentinel and its own reader names what it found
    (DL-138). The torn one is the residue this fix deliberately leaves: a
    half-written first line still reads as absence, and closing that needs a
    rule about what a torn sentinel MEANS, which no document states."""
    from dsl41.period import read_sentinel, sentinel_path

    future = tmp_path / "future"
    future.mkdir()
    sentinel_path(future).write_bytes(
        json.dumps(
            {"rec": "period_root", "artifact_format_version": 2, "estate_id": "e", "see": "wal/"},
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    with pytest.raises(EngineError, match="not a sentinel this binary can read") as refused:
        read_sentinel(future)
    assert "artifact_format_version 2" in str(refused.value)

    for index, line in enumerate(
        (b"not json at all\n", b'{"rec":"header"}\n', b'{"rec":"period_root"\n')
    ):
        other = tmp_path / f"other{index}"
        other.mkdir()
        sentinel_path(other).write_bytes(line)
        assert read_sentinel(other) is None


def test_pr08d_a_sentinel_missing_its_version_refuses_by_name(tmp_path: Path) -> None:
    """DL-157: protocol-evolution's closed-artifact row rules absence the
    same as an unimplemented version now -- a retained sentinel with no
    `artifact_format_version` key is corruption, not a v1 this reader is
    free to assume. The present-and-unsupported case is
    `test_pr08d_a_sentinel_this_binary_cannot_read_refuses_by_name`, above,
    unchanged by this fix."""
    from dsl41.canon import ARTIFACT_FORMAT_VERSION
    from dsl41.period import read_sentinel, sentinel_path

    missing = tmp_path / "missing-version"
    missing.mkdir()
    sentinel_path(missing).write_bytes(
        json.dumps({"rec": "period_root", "estate_id": "e", "see": "wal/"}, sort_keys=True).encode()
        + b"\n"
    )
    with pytest.raises(EngineError, match="missing artifact_format_version"):
        read_sentinel(missing)

    present = tmp_path / "present-version"
    present.mkdir()
    sentinel_path(present).write_bytes(
        json.dumps(
            {
                "rec": "period_root",
                "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                "estate_id": "e",
                "see": "wal/",
            },
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    sentinel = read_sentinel(present)
    assert sentinel is not None and sentinel.estate_id == "e"


def test_a_used_root_refuses_genesis_and_says_what_to_do(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    _close(_genesis(run_root))
    with pytest.raises(EngineError, match="already exists: resume it"):
        _genesis(run_root)


# ------------------------------------------------------ ss1.3 the anchor


def test_pr01b_genesis_against_an_existing_anchor_refuses(tmp_path: Path) -> None:
    """PR-01b: an existing anchor is an existing estate whose detached work
    may still be alive, whatever its incumbent's liveness says."""
    anchor_dir = tmp_path / "anchor"
    anchor = EstateAnchor(anchor_dir)
    anchor.acquire()
    anchor.create_open(estate_id="estate-one", root=tmp_path / "a")
    anchor.release()  # the incumbent is DEAD, and that changes nothing
    second = EstateAnchor(anchor_dir)
    second.acquire()
    with pytest.raises(EngineError, match="is not ours"):
        second.create_open(estate_id="estate-two", root=tmp_path / "b")
    second.release()


def test_pr01b_our_own_interrupted_genesis_is_the_one_resume(tmp_path: Path) -> None:
    anchor = EstateAnchor(tmp_path / "anchor")
    anchor.acquire()
    first = anchor.create_open(estate_id="e1", root=tmp_path / "r")
    assert isinstance(first.head, OpenHead)
    assert first.periods["1"].segment_durable is False  # provisional (PR-02c)
    again = anchor.create_open(estate_id="e1", root=tmp_path / "r")
    assert again == first
    # once the segment is durable, ordinary --resume owns recovery
    anchor.finalize(1)
    with pytest.raises(EngineError, match="is not ours"):
        anchor.create_open(estate_id="e1", root=tmp_path / "r")
    anchor.release()


def test_pr02c_the_registry_row_flips_in_the_finalize_cas(tmp_path: Path) -> None:
    anchor = EstateAnchor(tmp_path / "anchor")
    anchor.acquire()
    anchor.create_open(estate_id="e1", root=tmp_path / "r")
    assert anchor.read().periods["1"].segment_durable is False  # type: ignore[union-attr]
    anchor.finalize(1)
    assert anchor.read().periods["1"].segment_durable is True  # type: ignore[union-attr]
    anchor.finalize(1)  # idempotent: resume performs it without knowing genesis did
    anchor.release()


def test_pr02_the_claim_is_the_identity_not_the_process(tmp_path: Path) -> None:
    """PR-02: the same `(seal, next_period, root)` recomputes the same
    `claim_id` -- given as `./r` the first time and as an absolute path the
    second -- resumes it, and a different root still refuses."""
    root = tmp_path / "r"
    root.mkdir()
    digest = "sha256:" + "a" * 64
    anchor = EstateAnchor(tmp_path / "anchor")
    anchor.acquire()
    anchor.create_open(estate_id="e1", root=root)
    anchor.finalize(1)
    anchor.close_period(estate_id="e1", period_id=1, root=root, seal_digest=digest)
    claim = anchor.claim_successor(
        estate_id="e1", seal_digest=digest, next_period=2, target_root=root
    )
    # the SAME root, spelled the other way: a claimant started with a
    # relative or `..`-bearing path and restarted with the absolute one
    # must recompute the same claim, or an ordinary restart needs
    # break-glass. `os.path.realpath` is the one place the two meet.
    detoured = root.parent / "elsewhere" / ".." / root.name
    assert str(detoured) != str(root)
    assert claim.claim_id == claim_id_for(
        prev_seal_digest=digest, next_period=2, target_root=detoured
    )
    again = anchor.claim_successor(
        estate_id="e1", seal_digest=digest, next_period=2, target_root=root
    )
    assert again.claim_id == claim.claim_id  # idempotent on the claim
    with pytest.raises(EngineError, match="already claimed by"):
        anchor.claim_successor(
            estate_id="e1", seal_digest=digest, next_period=2, target_root=tmp_path / "other"
        )
    anchor.release()


def test_a_claim_missing_its_version_refuses_by_name(tmp_path: Path) -> None:
    """DL-157: the claim file is a closed artifact too (protocol-evolution's
    matrix), so an absent `artifact_format_version` refuses by name exactly
    as an unimplemented one does -- and still refuses an unimplemented one,
    unchanged."""
    from dsl41.boundary import CLAIMS_DIR
    from dsl41.canon import ARTIFACT_FORMAT_VERSION

    anchor_dir = tmp_path / "anchor"
    anchor = EstateAnchor(anchor_dir)
    claims_dir = anchor_dir / CLAIMS_DIR
    claims_dir.mkdir(parents=True)
    body = {
        "claim_id": "c1",
        "estate_id": "e1",
        "prev_seal_digest": "sha256:" + "a" * 64,
        "next_period": 2,
        "target_root": str(tmp_path / "r"),
        "claimed_at": "2026-08-24T00:00:00.000000",
        "diag": {},
    }

    def _write(name: str, doc: dict[str, Any]) -> None:
        (claims_dir / f"{name}.json").write_bytes(json.dumps(doc, sort_keys=True).encode() + b"\n")

    _write("missing-version", body)
    with pytest.raises(EngineError, match="missing artifact_format_version"):
        anchor.read_claim("missing-version")

    _write("present-version", {**body, "artifact_format_version": ARTIFACT_FORMAT_VERSION})
    claim = anchor.read_claim("present-version")
    assert claim is not None and claim.claim_id == "c1"

    _write("unsupported-version", {**body, "artifact_format_version": ARTIFACT_FORMAT_VERSION + 1})
    with pytest.raises(EngineError, match="not a claim this binary can read") as unsup:
        anchor.read_claim("unsupported-version")
    assert "missing artifact_format_version" not in str(unsup.value)


def test_pr05_an_estate_id_mismatch_refuses(tmp_path: Path) -> None:
    anchor = EstateAnchor(tmp_path / "anchor")
    anchor.acquire()
    anchor.create_open(estate_id="e1", root=tmp_path / "r")
    with pytest.raises(EngineError, match="two geneses are two estates"):
        anchor.require("e2")
    anchor.release()


def test_the_adopting_head_refuses_before_parse_and_names_the_dialect(tmp_path: Path) -> None:
    """D3 (DL-138): `adopting` is a RETIRED head state, and an anchor
    carrying one refuses BEFORE the discriminated union is parsed.

    Pydantic's own answer is "input tag 'adopting' ... does not match any of
    the expected tags", which tells an operator holding a pre-DL-138 anchor
    nothing about what they hold. An unknown state keeps that generic
    error -- "this used to be legal" and "this was never legal" are
    different facts (docs/protocol-evolution.md ss6)."""

    def _anchor_with(state: str) -> EstateAnchor:
        anchor_dir = tmp_path / state
        anchor_dir.mkdir()
        (anchor_dir / "anchor.json").write_bytes(
            json.dumps(
                {
                    "artifact_format_version": 1,
                    "estate_id": "e1",
                    "head": {"state": state, "period_id": 1, "root": str(tmp_path / "r")},
                    "periods": {},
                },
                sort_keys=True,
            ).encode()
            + b"\n"
        )
        return EstateAnchor(anchor_dir)

    retired = _anchor_with("adopting")
    with pytest.raises(EngineError, match="RETIRED") as named:
        retired.read()
    assert "DL-138" in str(named.value) and "`adopting`" in str(named.value)

    unknown = _anchor_with("dreaming")
    with pytest.raises(EngineError, match="not an anchor this binary can read") as generic:
        unknown.read()
    assert "DL-138" not in str(generic.value)


def test_an_anchor_missing_its_version_refuses_by_name(tmp_path: Path) -> None:
    """DL-157: the anchor is a closed artifact too (protocol-evolution's
    matrix), so an absent `artifact_format_version` refuses by name exactly
    as an unimplemented one does -- and still refuses an unimplemented one,
    unchanged."""
    from dsl41.boundary import ANCHOR_NAME
    from dsl41.canon import ARTIFACT_FORMAT_VERSION

    body = {
        "estate_id": "e",
        "head": {"state": "open", "period_id": 1, "root": str(tmp_path / "r")},
        "periods": {},
    }

    missing = EstateAnchor(tmp_path / "missing-version")
    missing.dir.mkdir()
    (missing.dir / ANCHOR_NAME).write_bytes(json.dumps(body, sort_keys=True).encode() + b"\n")
    with pytest.raises(EngineError, match="missing artifact_format_version"):
        missing.read()

    present = EstateAnchor(tmp_path / "present-version")
    present.dir.mkdir()
    (present.dir / ANCHOR_NAME).write_bytes(
        json.dumps(
            {**body, "artifact_format_version": ARTIFACT_FORMAT_VERSION}, sort_keys=True
        ).encode()
        + b"\n"
    )
    anchor = present.read()
    assert anchor is not None and anchor.estate_id == "e"

    unsupported = EstateAnchor(tmp_path / "unsupported-version")
    unsupported.dir.mkdir()
    (unsupported.dir / ANCHOR_NAME).write_bytes(
        json.dumps(
            {**body, "artifact_format_version": ARTIFACT_FORMAT_VERSION + 1}, sort_keys=True
        ).encode()
        + b"\n"
    )
    with pytest.raises(EngineError, match="not an anchor this binary can read") as unsup:
        unsupported.read()
    assert "missing artifact_format_version" not in str(unsup.value)


def test_pr04_a_network_filesystem_anchor_is_refused(tmp_path: Path, monkeypatch) -> None:
    """PR-04: flock on NFS is not a fence, and everything above the anchor
    is written as if it were one."""
    monkeypatch.setattr("dsl41.boundary.filesystem_type", lambda _path: "nfs4")
    with pytest.raises(EngineError, match="not a fence on a network filesystem"):
        EstateAnchor(tmp_path / "anchor").acquire()


def test_the_local_filesystem_check_reads_a_real_mount_table(tmp_path: Path) -> None:
    """The detector is not a stub: it answers for a real path, or says it
    cannot -- and `None` is an unknown, never a refusal."""
    kind = filesystem_type(tmp_path)
    assert kind is None or isinstance(kind, str)
    EstateAnchor(tmp_path / "anchor").acquire()  # a local temp dir is never refused


# --------------------------------------------------------- ss1.3 the fence


def test_pr03_the_anchor_deleted_under_a_live_incumbent_stops_it(tmp_path: Path) -> None:
    """PR-03: delete the anchor directory under a live incumbent and it
    stops on its next append -- DL-101's bargain, which turns a divergence
    into a recorded stop."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    (default_anchor_dir(run_root) / "anchor.lock").unlink()
    with pytest.raises(EngineError, match="can no longer prove it leads"):
        engine.journal.leader(epoch=99, at=T0)  # type: ignore[union-attr]
    _close(engine)


def test_pr03_a_replaced_anchor_lock_stops_the_next_append(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    lock_path = default_anchor_dir(run_root) / "anchor.lock"
    lock_path.unlink()
    lock_path.write_text("")  # replaced under the pathname: our lock excludes nobody
    with pytest.raises(EngineError, match="was replaced"):
        engine.journal.leader(epoch=99, at=T0)  # type: ignore[union-attr]
    _close(engine)


def test_the_run_root_lock_is_still_proved_beside_the_anchor(tmp_path: Path) -> None:
    """The fence is BOTH proofs: a writer typed to one lock could only ever
    check one of them."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    (run_root / "leader.lock").unlink()
    with pytest.raises(EngineError, match="leader.lock was deleted"):
        engine.journal.leader(epoch=99, at=T0)  # type: ignore[union-attr]
    _close(engine)


# ---------------------------------------------------------- ss9 the gate


def _attempt(index: int, at: datetime, *, expect: dict[str, int] | None) -> dict[str, Any]:
    """One attempt record, in the shape `Journal.admit` writes -- `seq` and
    NOT `index`, which is the decision's key. A helper that wrote both
    would let the gate read the wrong one and still pass."""
    record: dict[str, Any] = {"rec": "input", "seq": index, "at": at.isoformat()}
    if expect is not None:
        record["expect"] = expect
    return record


def _decision(index: int, decision: str = "applied") -> dict[str, Any]:
    return {"rec": "decision", "index": index, "decision": decision}


def test_pr30_the_gate_passes_with_no_externally_requested_attempt() -> None:
    """ss3.1's truth table, first row: age = infinity, the gate passes and
    `forced_gate` is null whatever `force_seal` says."""
    records = [_attempt(1, T0, expect=None), _decision(1)]
    assert retry_horizon_gate(records, horizon_us=60_000_000, at=T0, force_seal=False) is None
    assert retry_horizon_gate(records, horizon_us=60_000_000, at=T0, force_seal=True) is None


def test_pr30_an_unnecessary_force_engages_no_gate() -> None:
    records = [_attempt(1, T0, expect={"job:a": 1}), _decision(1)]
    late = T0 + timedelta(seconds=120)
    assert retry_horizon_gate(records, horizon_us=60_000_000, at=late, force_seal=True) is None


def test_pr30_a_recent_attempt_refuses_unforced_and_records_the_gate_when_forced() -> None:
    """ "Attempt", not "mutation": a `rejected` CAS loser two seconds ago
    holds the gate exactly as an applied STARTJOB would."""
    for decision in ("applied", "rejected"):
        records = [_attempt(1, T0, expect={"job:a": 1}), _decision(1, decision)]
        at = T0 + timedelta(seconds=2)
        with pytest.raises(EngineError, match="retry_horizon_us"):
            retry_horizon_gate(records, horizon_us=60_000_000, at=at, force_seal=False)
        gate = retry_horizon_gate(records, horizon_us=60_000_000, at=at, force_seal=True)
        assert gate is not None
        assert gate.gate == "retry_horizon" and gate.observed_age_us == 2_000_000


def test_an_admitted_attempt_with_no_durable_decision_does_not_hold_the_gate() -> None:
    records = [_attempt(1, T0, expect={"job:a": 1})]  # admitted, undecided
    at = T0 + timedelta(seconds=2)
    assert retry_horizon_gate(records, horizon_us=60_000_000, at=at, force_seal=False) is None


# --------------------------------------------- ss6/ss7 the boundary proper


def test_a_quiet_boundary_commits_and_the_engine_asks_to_be_reopened(tmp_path: Path) -> None:
    """The whole operation, end to end: the three writes in order, the head
    closed, the candidate installed, and an engine that exits `PeriodSealed`
    rather than returning."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)
    boundary = asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)

    seal = boundary.seal
    assert seal.period_id == 1 and seal.next_period.period_id == 2
    assert seal.next_period.first_index == seal.closes_at_index + 1  # PR-05b
    assert seal.boundary_request.claimed_actor == "alice@ops"
    assert seal.forced_gate is None
    # 1. the sidecar, 2. the record, 3. the anchor CAS -- all durable
    assert read_seal(run_root, 1).digest == seal.digest
    records = read_journal(wal_path(run_root, 1))
    assert records[-1]["rec"] == "seal" and records[-1]["digest"] == seal.digest
    anchor = EstateAnchor(default_anchor_dir(run_root))
    head = anchor.read().head  # type: ignore[union-attr]
    assert isinstance(head, ClosedHead) and head.seal_digest == seal.digest
    # the candidate is installed at its committed name, both files kept
    installed = period_dir(run_root, 2)
    assert (installed / "manifest.json").exists()
    assert (installed / "staged_manifest.json").exists()
    assert read_candidate(installed).stage_digest == staged.stage_digest  # type: ignore[union-attr]


def test_pr29_the_old_period_admits_nothing_after_its_seal(tmp_path: Path) -> None:
    """A record after a `seal` in one segment is refused at the read, not
    tolerated as a torn tail."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    path = wal_path(run_root, 1)
    path.write_text(path.read_text() + json.dumps({"rec": "drop", "at": T0.isoformat()}) + "\n")
    with pytest.raises(EngineError, match="after the .seal."):
        read_journal(path)


def test_pr28e_admission_closes_at_the_cut(tmp_path: Path) -> None:
    """ss6 step 2 freezes EVERY externally requested attempt -- rejected and
    no-op ones included, since each takes a durable decision."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    engine.sealing = True
    from dsl41.runner_admission import AdmissionRefused, Envelope

    async def scenario() -> None:
        future = engine.submit(
            Event(at=T0, kind="STARTJOB", payload={"job": "a"}),
            Envelope(request_id="r1", expect={"job:a": 0}, epoch=engine.epoch),
        )
        with pytest.raises(AdmissionRefused, match="this period is sealing"):
            await future
        assert not engine._queue  # nothing admitted: no index, no record
        # the engine's OWN door stays open: the drain has to finish
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "a"}), source="scheduler")
        assert engine._queue

    asyncio.run(scenario())
    _close(engine)


def test_pr28_readiness_refuses_while_c1_is_open_and_untouched(tmp_path: Path) -> None:
    """One injected failure per phase-1 check, each refusing with the
    message only its own rule produces -- and C1 still running afterwards."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)

    # the request's own digest must agree with its candidate
    other = _stage(run_root, C1_JIL)
    message = asyncio.run(
        _refused(engine, _request(engine, staged, stage_digest=other.stage_digest))
    )
    assert "is not the one the request names" in message
    # the state-machine version is not a client's to choose: staged for
    # real, so the refusal is the SM gate's and not the staging gate's
    catalog, sources = _catalog(C2_JIL, name="v2.jil")
    bumped_manifest = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION + 1,
    )
    bumped = stage_next_period(run_root, staged_manifest=bumped_manifest)
    message = asyncio.run(_refused(engine, _request(engine, bumped)))
    assert "one executable implements one version" in message
    # and the engine is still open for business afterwards (PR-28b)
    engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "a"}))
    assert asyncio.run(engine.run_until_quiescent(T0)) != []
    _close(engine)


# ------------------------------------------------- ss11 the opening half


def _closed_again(stored: Anchor) -> ClosedHead:
    """Put the head back where the crash-before-the-segment row leaves it."""
    row = stored.periods["1"]
    assert row.seal_digest is not None
    return ClosedHead(period_id=1, seal_digest=row.seal_digest, closing_root=row.root)


def _no_crash(_stage: str) -> None:
    """The production hook: a no-op."""


def _crash_at(stage: str):
    """The boundary's own seam (`Engine.crash_point`), stopped at one
    named durable step."""

    def hook(name: str) -> None:
        if name == stage:
            raise EngineError(f"crash at {name}")

    return hook


def _resume(run_root: Path, text: str, *, clock: VirtualClock | None = None):
    catalog, _ = _catalog(text)
    return asyncio.run(
        resume_run(
            catalog,
            run_root,
            clock=clock or VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
    )


def test_the_boundary_opens_in_place_and_the_carry_survives(tmp_path: Path) -> None:
    """ss11 steps 3-5, in place: the seal is selected by lineage, the
    successor is claimed, the opening segment is written at T, and the
    carried rows install VERBATIM -- revisions included."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)

    async def under_c1() -> None:
        engine.inject(Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": "1"}))
        engine.inject(Event(at=T0, kind="ON_HOLD", payload={"job": "b"}))
        await engine.run_until_quiescent(T0)

    asyncio.run(under_c1())
    revision = engine.oracle.store.revision("global:G")
    held = engine.oracle.store.runtime("b").on_hold
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)

    opened = _resume(run_root, C2_JIL)
    assert opened.oracle.store.period_id == 2
    assert opened.baseline_id == boundary.seal.next_period.baseline_id
    # PR-23/ss7 step 3: the carried row is installed, not re-seeded
    assert opened.oracle.store.global_value("G") == "1"
    assert opened.oracle.store.revision("global:G") == revision
    assert opened.oracle.store.runtime("b").on_hold is held is True
    # I2: the epoch is estate-monotone, and the index continues
    assert opened.epoch == boundary.seal.epoch + 1
    assert opened.frontiers.committed_index == boundary.seal.closes_at_index
    segment = read_journal(wal_path(run_root, 2))[0]
    assert segment["at"] == boundary.seal.closed_at.isoformat()  # `at` IS T
    assert segment["opens_from_seal"] == {"period_id": 1, "digest": boundary.seal.digest}
    assert segment["first_index"] == boundary.seal.closes_at_index + 1
    anchor = EstateAnchor(default_anchor_dir(run_root))
    stored = anchor.read()
    assert stored is not None and isinstance(stored.head, OpenHead)
    assert stored.head.period_id == 2 and stored.periods["2"].segment_durable is True
    _close(opened)


def test_pr07_two_openings_of_one_seal_are_byte_identical(tmp_path: Path) -> None:
    """PR-07's operational half: reopening the same seal produces the same
    `segment` record, byte for byte. `at` is T rather than restart wall
    time, and `next_period` commits every non-derived opening field."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)

    first = _resume(run_root, C2_JIL)
    _close(first)
    bytes_one = wal_path(run_root, 2).read_bytes().splitlines()[0]
    # throw the opening away and open the SAME seal again, at another wall
    # instant and under another leader term
    wal_path(run_root, 2).unlink()
    anchor = EstateAnchor(default_anchor_dir(run_root))
    anchor.acquire()
    stored = anchor.read()
    assert stored is not None
    anchor.write(stored.model_copy(update={"head": _closed_again(stored)}))
    anchor.release()
    second = _resume(run_root, C2_JIL, clock=VirtualClock(start=T0 + timedelta(hours=3)))
    _close(second)
    assert wal_path(run_root, 2).read_bytes().splitlines()[0] == bytes_one


def test_pr02b_the_open_to_closed_cas_is_performed_at_resume(tmp_path: Path) -> None:
    """ss11's matrix row: the `seal` record landed and the head still says
    `open`. Resume performs the CAS, and the successor claim then
    proceeds."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    engine.crash_point = _crash_at("after_seal_record")  # type: ignore[method-assign]
    request = _request(engine, _stage(run_root, C2_JIL))

    async def scenario() -> None:
        future = engine.submit_seal(request)
        with pytest.raises(EngineError, match="did not complete cleanly"):
            await engine.run_until_quiescent(T0)
        assert future.done() is False or future.exception() is not None

    asyncio.run(scenario())
    _close(engine)
    anchor = EstateAnchor(default_anchor_dir(run_root))
    stored = anchor.read()
    assert stored is not None and isinstance(stored.head, OpenHead)  # the CAS never ran
    opened = _resume(run_root, C2_JIL)
    _close(opened)
    assert opened.oracle.store.period_id == 2
    reread = EstateAnchor(default_anchor_dir(run_root)).read()
    assert reread is not None and isinstance(reread.head, OpenHead)
    assert reread.head.period_id == 2


def test_pr45_a_claim_with_a_durable_segment_moves_the_head_at_resume(tmp_path: Path) -> None:
    """ss11's matrix row: `claimed` with our `claim_id` and our first
    segment ALREADY durable -- the crash was between the segment and the
    head."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    first = _resume(run_root, C2_JIL)
    _close(first)
    # rewind the head to `claimed`, leaving the durable segment in place
    seal = read_seal(run_root, 1)
    anchor = EstateAnchor(default_anchor_dir(run_root))
    anchor.acquire()
    stored = anchor.read()
    assert stored is not None
    anchor.write(
        stored.model_copy(
            update={
                "head": ClaimedHead(
                    claim_id=claim_id_for(
                        prev_seal_digest=seal.digest, next_period=2, target_root=run_root
                    ),
                    target_root=str(run_root.resolve()),
                )
            }
        )
    )
    anchor.release()
    second = _resume(run_root, C2_JIL)
    _close(second)
    stored = EstateAnchor(default_anchor_dir(run_root)).read()
    assert stored is not None and isinstance(stored.head, OpenHead)
    assert stored.head.period_id == 2


#: the five fields the DL-137 derived set replaced. Named here because the
#: test below drives a field that is NOT among them: it is what makes the
#: case a pin on the DERIVED set rather than on any five fields
_HAND_WRITTEN_FIVE = ("period_id", "segment_no", "baseline_id", "first_index", "clock_domain")


@contextlib.contextmanager
def _crashed_between_the_segment_and_the_cas(run_root: Path):
    """The window `_check_existing_segment` exists for, built natively: an
    opener that took the claim, wrote the opening segment and died before
    the head CAS.

    The opener's OWN seam makes the state rather than anchor surgery, so
    what the retry meets is what a real crash leaves. The anchor lock is
    held around the body, because the retry re-enters the opener where
    recovery does -- under the lineage lock."""
    engine = _genesis(run_root)
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    catalog, _ = _catalog(C2_JIL)
    manifest = read_period_manifest(run_root, 2)
    assert manifest is not None
    # read back off the disk, never re-derived: the retry knows the seal
    # the way recovery knows it
    committed = CommittedBoundary(seal=read_seal(run_root, 1), manifest=manifest)
    assert committed.seal.digest == boundary.seal.digest
    anchor = EstateAnchor(default_anchor_dir(run_root))
    anchor.acquire()
    try:
        with pytest.raises(EngineError, match="crash at after_opening_segment"):
            open_next_period(
                run_root=run_root,
                anchor=anchor,
                committed=committed,
                catalog=catalog,
                crash_point=_crash_at("after_opening_segment"),
            )
        stored = anchor.require(committed.seal.estate_id)
        assert isinstance(stored.head, ClaimedHead)  # the CAS never ran
        assert wal_path(run_root, 2).exists()  # and the segment is durable
        yield anchor, committed, catalog
    finally:
        anchor.release()


def test_an_existing_next_segment_that_agrees_is_verified_and_the_cas_runs(
    tmp_path: Path,
) -> None:
    """The positive half of the same rule: the segment the crashed opener
    wrote agrees with the committed boundary field for field, so the retry
    VERIFIES it and goes on to the head CAS.

    Without this case the refusal below could be the fixture's rather than
    the rule's -- and the file is neither rewritten nor appended to, which
    is I1 (one segment record, one period)."""
    run_root = tmp_path / "run"
    with _crashed_between_the_segment_and_the_cas(run_root) as (anchor, committed, catalog):
        written = wal_path(run_root, 2).read_bytes()
        opened = open_next_period(
            run_root=run_root, anchor=anchor, committed=committed, catalog=catalog
        )
        opened.journal.close()
    stored = EstateAnchor(default_anchor_dir(run_root)).read()
    assert stored is not None and isinstance(stored.head, OpenHead)
    assert stored.head.period_id == 2  # the CAS the crash cost us
    assert wal_path(run_root, 2).read_bytes() == written
    assert [r["rec"] for r in read_journal(wal_path(run_root, 2))].count("segment") == 1


def test_an_existing_next_segment_that_disagrees_refuses_before_the_cas(tmp_path: Path) -> None:
    """Presence is not agreement: the full seal-to-opening validation runs
    BEFORE the recovery CAS, so a segment whose pins disagree refuses with
    the head still `claimed` -- rather than after the head has named it.

    `runtime_hash` is driven on purpose. The check compares the DERIVED
    `model_fields & SEGMENT_FIELDS` set (DL-137), and the hand-written five
    it replaced did not include it: the same segment passed the old check
    and is refused by this one."""
    run_root = tmp_path / "run"
    stranger = "sha256:" + "b" * 64
    with _crashed_between_the_segment_and_the_cas(run_root) as (anchor, committed, catalog):
        opening = committed.seal.next_period
        derived = set(type(opening).model_fields) & SEGMENT_FIELDS
        assert "runtime_hash" in derived and "runtime_hash" not in _HAND_WRITTEN_FIVE
        assert opening.runtime_hash != stranger
        path = wal_path(run_root, 2)
        lines = path.read_text().splitlines()
        segment = {**json.loads(lines[0]), "runtime_hash": stranger}
        path.write_text("\n".join([json.dumps(segment, sort_keys=True), *lines[1:]]) + "\n")

        with pytest.raises(EngineError, match="did not open") as refused:
            open_next_period(run_root=run_root, anchor=anchor, committed=committed, catalog=catalog)
        message = str(refused.value)
        stored = anchor.require(committed.seal.estate_id)
    assert f"runtime_hash: segment {stranger!r}" in message  # the field, and both sides
    assert repr(opening.runtime_hash) in message
    assert message.count(" vs the boundary's ") == 1  # that field alone
    assert "ss3.4" in message
    assert isinstance(stored.head, ClaimedHead)  # the CAS never ran
    reread = EstateAnchor(default_anchor_dir(run_root)).read()
    assert reread is not None and isinstance(reread.head, ClaimedHead)


#: a scheduled estate, so a tick can LATCH on a held job (SEM-32)
SCHED_C1 = (
    "insert_job: a\njob_type: c\ncommand: x\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
)
SCHED_C2 = SCHED_C1 + "\ninsert_job: z\njob_type: c\ncommand: new\n"


def _scheduler(text: str, at: datetime):
    from dsl41.runner_scheduler import Scheduler

    catalog, _ = _catalog(text)
    return Scheduler(catalog, start=at)


def test_pr26_one_held_tick_under_c1_starts_exactly_once_after_c2(tmp_path: Path) -> None:
    """ss10.4: armed latches cross a release, deliberately -- dropping one
    at the boundary would be an implicit transition with no admitted input.
    One tick under C1 while the job is held; the operator's `OFF_HOLD` in
    C2 produces EXACTLY ONE start (PR-26)."""
    run_root = tmp_path / "run"
    catalog, sources = _catalog(SCHED_C1)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    clock = VirtualClock(start=T0)
    engine = start_run(
        catalog,
        run_root,
        clock=clock,
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=_scheduler(SCHED_C1, T0),
        staged=staged,
    )

    async def under_c1() -> None:
        engine.inject(Event(at=T0, kind="ON_HOLD", payload={"job": "a"}))
        await engine.run_until_quiescent(T0)
        await engine.run_until_quiescent(datetime(2026, 7, 1, 9, 30))  # the 09:00 tick

    asyncio.run(under_c1())
    assert engine.oracle.store.runtime("a").armed is True  # latched, not started
    assert engine.oracle.store.runtime("a").run_number == 0
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, SCHED_C2))))
    _close(engine)

    catalog2, _ = _catalog(SCHED_C2)
    opened = asyncio.run(
        resume_run(
            catalog2,
            run_root,
            clock=VirtualClock(start=boundary.seal.closed_at),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=_scheduler(SCHED_C2, boundary.seal.closed_at),
        )
    )
    assert opened.oracle.store.runtime("a").armed is True  # the latch crossed

    async def under_c2() -> None:
        opened.inject(Event(at=boundary.seal.closed_at, kind="OFF_HOLD", payload={"job": "a"}))
        await opened.run_until_quiescent(boundary.seal.closed_at)

    asyncio.run(under_c2())
    assert opened.oracle.store.runtime("a").run_number == 1
    assert opened.oracle.store.runtime("a").armed is False
    starts = [
        effect
        for record in read_journal(wal_path(run_root, 2))
        if record.get("rec") == "decision"
        for effect in record["effects"]
        if effect["kind"] == "SPAWN" and effect["job"] == "a"
    ]
    assert len(starts) == 1  # exactly one, and it belongs to C2
    _close(opened)


def test_dl158_disarm_before_the_seal_yields_no_start_after_c2(tmp_path: Path) -> None:
    """ss10.4's other half (DL-158): the explicit journaled disarm BEFORE
    the seal is the honest alternative to a boundary drop. Same run as the
    PR-26 twin above -- held job, one tick latches -- but the operator sends
    DISARM before sealing: the latch does NOT cross, C2's OFF_HOLD starts
    NOTHING, and the new period's WAL carries no SPAWN for the job."""
    run_root = tmp_path / "run"
    catalog, sources = _catalog(SCHED_C1)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    clock = VirtualClock(start=T0)
    engine = start_run(
        catalog,
        run_root,
        clock=clock,
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=_scheduler(SCHED_C1, T0),
        staged=staged,
    )

    async def under_c1() -> None:
        engine.inject(Event(at=T0, kind="ON_HOLD", payload={"job": "a"}))
        await engine.run_until_quiescent(T0)
        await engine.run_until_quiescent(datetime(2026, 7, 1, 9, 30))  # the 09:00 tick
        assert engine.oracle.store.runtime("a").armed is True  # latched, not started
        disarm_at = datetime(2026, 7, 1, 9, 40)
        engine.inject(Event(at=disarm_at, kind="DISARM", payload={"job": "a"}))
        await engine.run_until_quiescent(disarm_at)

    asyncio.run(under_c1())
    assert engine.oracle.store.runtime("a").armed is False  # dropped before the seal
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, SCHED_C2))))
    _close(engine)

    catalog2, _ = _catalog(SCHED_C2)
    opened = asyncio.run(
        resume_run(
            catalog2,
            run_root,
            clock=VirtualClock(start=boundary.seal.closed_at),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=_scheduler(SCHED_C2, boundary.seal.closed_at),
        )
    )
    assert opened.oracle.store.runtime("a").armed is False  # nothing crossed

    async def under_c2() -> None:
        opened.inject(Event(at=boundary.seal.closed_at, kind="OFF_HOLD", payload={"job": "a"}))
        await opened.run_until_quiescent(boundary.seal.closed_at)

    asyncio.run(under_c2())
    assert opened.oracle.store.runtime("a").run_number == 0  # no start: the drop held
    starts = [
        effect
        for record in read_journal(wal_path(run_root, 2))
        if record.get("rec") == "decision"
        for effect in record["effects"]
        if effect["kind"] == "SPAWN" and effect["job"] == "a"
    ]
    assert starts == []  # C2 produced no SPAWN for the disarmed job
    _close(opened)


def test_dl158_disarm_composed_in_c2_drops_the_carried_latch_no_start(tmp_path: Path) -> None:
    """ss10.4's boundary case (DL-158): the latch crosses the seal, and a
    NEWLY COMPOSED C2 command may drop it -- the disarm is legal on either
    side of the seal. Same run as the pre-seal twin above, but the DISARM
    is sent in the new period, after resume and before the OFF_HOLD: the
    carried latch drops, the OFF_HOLD starts nothing."""
    run_root = tmp_path / "run"
    catalog, sources = _catalog(SCHED_C1)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    clock = VirtualClock(start=T0)
    engine = start_run(
        catalog,
        run_root,
        clock=clock,
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=_scheduler(SCHED_C1, T0),
        staged=staged,
    )

    async def under_c1() -> None:
        engine.inject(Event(at=T0, kind="ON_HOLD", payload={"job": "a"}))
        await engine.run_until_quiescent(T0)
        await engine.run_until_quiescent(datetime(2026, 7, 1, 9, 30))  # the 09:00 tick

    asyncio.run(under_c1())
    assert engine.oracle.store.runtime("a").armed is True  # latched, not started
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, SCHED_C2))))
    _close(engine)

    catalog2, _ = _catalog(SCHED_C2)
    opened = asyncio.run(
        resume_run(
            catalog2,
            run_root,
            clock=VirtualClock(start=boundary.seal.closed_at),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=_scheduler(SCHED_C2, boundary.seal.closed_at),
        )
    )
    assert opened.oracle.store.runtime("a").armed is True  # the latch crossed

    async def under_c2() -> None:
        at = boundary.seal.closed_at
        opened.inject(Event(at=at, kind="DISARM", payload={"job": "a"}))  # C2-composed
        await opened.run_until_quiescent(at)
        assert opened.oracle.store.runtime("a").armed is False  # carried latch dropped
        opened.inject(Event(at=at, kind="OFF_HOLD", payload={"job": "a"}))
        await opened.run_until_quiescent(at)

    asyncio.run(under_c2())
    assert opened.oracle.store.runtime("a").run_number == 0  # no start: the drop held
    starts = [
        effect
        for record in read_journal(wal_path(run_root, 2))
        if record.get("rec") == "decision"
        for effect in record["effects"]
        if effect["kind"] == "SPAWN" and effect["job"] == "a"
    ]
    assert starts == []  # C2 produced no SPAWN for the disarmed job
    _close(opened)


# ------------------------------------------------- ss7 the crash matrix


#: the durable steps of ss7's write order, in order. The first three are
#: inside the REVERSIBLE interval -- every non-commit exit there runs
#: `abort_boundary` -- and the last two are past the point of no return.
_PRE_COMMIT = ("after_committed_manifest", "after_install", "after_sidecar")


@pytest.mark.parametrize("stage", _PRE_COMMIT)
def test_pr28b_every_non_commit_exit_before_the_append_aborts_and_retries(
    tmp_path: Path, stage: str
) -> None:
    """PR-28b: after EVERY non-commit exit before the `seal` append,
    `abort_boundary` has run -- a control command is admitted, a scheduled
    tick fires, an FW poll appends -- and the retry commits.

    Draft 21 ran the abort only on validation failure, and an `ENOSPC` on
    the sidecar left a live engine frozen behind a freeze it would never
    lift."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)
    engine.crash_point = _crash_at(stage)  # type: ignore[method-assign]
    message = asyncio.run(_refused(engine, _request(engine, staged)))
    assert f"crash at {stage}" in message
    assert engine.sealing is False and engine.barrier.parked is False
    # C1 is open and correct: an ordinary input is admitted and applies
    engine.inject(Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": "after"}))
    asyncio.run(engine.run_until_quiescent(T0))
    assert engine.oracle.store.global_value("G") == "after"
    # and the SAME request retries: nothing named the first attempt, so its
    # retry is a fresh request that attempts the boundary again (PR-30a)
    engine.crash_point = _no_crash  # type: ignore[method-assign]
    boundary = asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)
    assert boundary.seal.next_period.stage_digest == staged.stage_digest
    assert read_seal(run_root, 1).digest == boundary.seal.digest


def test_pr30f_a_retry_after_the_install_regenerates_the_manifest(tmp_path: Path) -> None:
    """PR-30d/PR-30f: the engine dies after installing `periods/000002/` and
    before the `seal` record. A retry whose `stage_digest` equals
    `candidate.json`'s reuses the STAGED IDENTITY and regenerates
    `manifest.json` from its OWN cutoff -- `first_index` is attempt output,
    and the first attempt's index is not the retry's truth."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)
    engine.crash_point = _crash_at("after_install")  # type: ignore[method-assign]
    asyncio.run(_refused(engine, _request(engine, staged)))
    installed = period_dir(run_root, 2)
    first_index = read_period_manifest(run_root, 2).first_index  # type: ignore[union-attr]
    assert read_candidate(installed).stage_digest == staged.stage_digest  # type: ignore[union-attr]
    # an intervening indexed C1 admission moves the cutoff
    engine.inject(Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": "1"}))
    asyncio.run(engine.run_until_quiescent(T0))
    engine.crash_point = _no_crash  # type: ignore[method-assign]
    boundary = asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)
    committed = read_period_manifest(run_root, 2)
    assert committed is not None and committed.first_index > first_index
    assert committed.first_index == boundary.seal.closes_at_index + 1
    # both files survive the reuse path: recovery is decided by them
    assert (installed / "staged_manifest.json").exists()
    assert (installed / "candidate.json").exists()


def test_pr30d_a_retry_with_another_digest_quarantines_the_installed_candidate(
    tmp_path: Path,
) -> None:
    """PR-30d: a retry differing in a staged field moves the installed
    candidate to `periods/.quarantine/<old digest>/<sha256 of its
    manifest>/` and installs its own, so a stale candidate is never
    silently selected."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    first = _stage(run_root, C2_JIL)
    engine.crash_point = _crash_at("after_install")  # type: ignore[method-assign]
    asyncio.run(_refused(engine, _request(engine, first)))
    assert read_candidate(period_dir(run_root, 2)).stage_digest == first.stage_digest  # type: ignore[union-attr]

    second = _stage(run_root, C1_JIL)  # a different staged identity
    assert second.stage_digest != first.stage_digest
    engine.crash_point = _no_crash  # type: ignore[method-assign]
    boundary = asyncio.run(_seal(engine, _request(engine, second)))
    _close(engine)
    assert boundary.seal.next_period.stage_digest == second.stage_digest
    assert read_candidate(period_dir(run_root, 2)).stage_digest == second.stage_digest  # type: ignore[union-attr]
    quarantined = list((run_root / "periods" / ".quarantine").rglob("candidate.json"))
    assert len(quarantined) == 1
    assert read_candidate(quarantined[0].parent).stage_digest == first.stage_digest  # type: ignore[union-attr]


def test_pr28d_a_failure_at_the_seal_append_fail_stops_without_reopening(
    tmp_path: Path,
) -> None:
    """PR-28d: once any seal bytes may have been written the engine
    fail-stops and reports the outcome UNKNOWN. It never aborts: reopening
    C1 would append commands, ticks and completions after a seal line."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    engine.crash_point = _crash_at("after_seal_record")  # type: ignore[method-assign]
    request = _request(engine, _stage(run_root, C2_JIL))

    async def scenario() -> None:
        engine.submit_seal(request)
        with pytest.raises(EngineError) as stopped:
            await engine.run_until_quiescent(T0)
        assert "the outcome is UNKNOWN" in str(stopped.value)
        assert request.request_id in str(stopped.value)

    asyncio.run(scenario())
    assert engine.sealing is True  # admission stays closed
    assert engine.barrier.parked is True
    _close(engine)


# --------------------------------------------------- ss8 preconditions


def _start_a(engine, at: datetime = T0) -> None:
    engine.inject(Event(at=at, kind="STARTJOB", payload={"job": "a"}))
    asyncio.run(engine.run_until_quiescent(at))


def test_pr27_an_applied_spawn_with_no_spool_binding_refuses(tmp_path: Path) -> None:
    """ss8: every applied CMD SPAWN is bound or terminal before the seal
    commits. There is no applied-but-unbound execution kind -- the sealer
    waits it out, and refuses rather than inventing a fourth."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    engine.QUIESCE_WAIT_S = 0.05  # the wait is real; the bound is what refuses
    _start_a(engine)
    assert engine.oracle.store.runtime("a").status == "RUNNING"
    message = asyncio.run(_refused(engine, _request(engine, _stage(run_root, C2_JIL))))
    assert "an applied SPAWN with no spawn.json" in message
    _close(engine)


def test_pr32_the_seal_names_the_executor_run_id_and_generation_of_every_live_run(
    tmp_path: Path,
) -> None:
    """PR-32: from the seal ALONE, with the supervisor answering nothing,
    every live run is named -- executor, `run_id` and generation, plus the
    spool binding that says which directory holds it."""
    from dsl41.period import read_period_manifest as _manifest
    from dsl41.seal import open_from_seal

    run_root = tmp_path / "run"
    engine = _genesis(run_root, profile=DETACHED)
    _start_a(engine)
    effect = next(e for e in engine.outbox.effects() if e.kind == "SPAWN")
    run_dir = run_root / "runs" / "a.1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "spawn.json").write_text(json.dumps({"run_id": effect.run_id}))
    engine.supervisor = _StubSupervisor(  # ss8: a detached seal proves its supervisor
        listing={
            "incarnation": "inc-1",
            "runs": [{"run_id": effect.run_id, "job": "a", "run_number": 1, "wrapper_alive": True}],
        }
    )  # type: ignore[assignment]
    boundary = asyncio.run(
        _seal(engine, _request(engine, _stage(run_root, C2_JIL, profile=DETACHED)))
    )
    _close(engine)

    entry = boundary.seal.executions[0]
    assert entry.kind == "bound"
    assert entry.run_id == effect.run_id
    assert entry.executor_id == effect.executor_id
    assert entry.generation == (effect.generation or 0)
    assert entry.run_dir == "runs/a.1"  # RELATIVE to the estate root (ss3.5)
    opened = open_from_seal(
        read_seal(run_root, 1),
        expected_digest=boundary.seal.digest,
        manifest=_manifest(run_root, 2),  # type: ignore[arg-type]
    )
    assert [e.run_id for e in opened.executions] == [effect.run_id]
    assert opened.dispatched == {"a": 1}  # ss3.3's ghost-run gate, rebuilt


def test_the_carried_execution_sets_come_from_the_wal_alone(tmp_path: Path) -> None:
    """ss10.1's executing tier needs no spool: readiness runs BEFORE the
    sealer has waited an unbound SPAWN out, and a classifier that needed
    `spawn.json` could not run there at all."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    _start_a(engine)
    assert executing_jobs(engine.outbox, engine.oracle.store.job) == {"a": "applied"}
    _close(engine)


# ---------------------------------------------- ss3.5 the FW seal barrier


def _fw_estate(run_root: Path, watch_file: Path, *, interval: int = 60, min_size: int = 5):
    text = (
        f"insert_job: w\njob_type: f\nwatch_file: {watch_file}\n"
        f"watch_interval: {interval}\nwatch_file_min_size: {min_size}\n"
    )
    catalog, sources = _catalog(text, name="fw.jil")
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    from dsl41.runner_adapters import FileWatcherAdapter

    engine = start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"FW": FileWatcherAdapter()},
        staged=staged,
    )
    engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "w"}))
    return engine, text


def _watch_lines(run_root: Path) -> list[dict[str, Any]]:
    raw = (run_root / "runs" / "w.1" / "watch.jsonl").read_bytes()
    return [json.loads(line) for line in raw.split(b"\n") if line]


def test_pr34_the_barrier_parks_every_fw_task_at_a_poll_boundary(tmp_path: Path) -> None:
    """ss3.5's seal barrier: at ss6 step 2 the engine parks every FW task at
    a poll boundary, BEFORE T is chosen.

    Without it a second qualifying poll lands after the snapshot, its
    completion is never admitted because the engine exits, and audit
    derives a completed watch where the seal carries a live one. Driven in
    ONE event loop, because a fresh `asyncio.run` cancels the live task and
    would prove the property vacuously."""
    run_root = tmp_path / "run"
    watch_file = tmp_path / "watched"
    watch_file.write_text("xxxxxxxx")
    engine, text = _fw_estate(run_root, watch_file, min_size=99)  # never qualifies: stays live
    staged_text = text + "\ninsert_job: q\njob_type: c\ncommand: x\n"

    async def scenario() -> None:
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        before = len(_watch_lines(run_root))
        assert before == 2  # the `start` line plus the immediate first poll
        staged = _stage(run_root, staged_text, name="fw.jil")
        boundary = await _seal(engine, _request(engine, staged))
        after = _watch_lines(run_root)
        assert len(after) == before  # no C1 line lands after the snapshot
        entry = boundary.seal.executions[0]
        assert entry.kind == "fw_watch"
        assert entry.watch_seq == before  # the prefix is named by a LINE COUNT
        assert entry.previous_size is None and entry.stable_polls == 0
        # ss3.5's two timestamps, asserted directly rather than through the
        # helper that computes them: after a poll line, `poll.at + interval`
        assert entry.next_poll_at == datetime.fromisoformat(after[-1]["at"]) + timedelta(seconds=60)
        assert engine.barrier.parked is True
        # the watch is parked, not finished: the next period resumes it from
        # exactly the line the seal counted
        assert "w" in engine.live_jobs()

    asyncio.run(scenario())
    _close(engine)


def test_pr28b_an_abort_unparks_the_watch(tmp_path: Path) -> None:
    """PR-28b's third clause: after an abort an FW poll appends again."""
    run_root = tmp_path / "run"
    watch_file = tmp_path / "watched"
    watch_file.write_text("xxxxxxxx")
    engine, text = _fw_estate(run_root, watch_file, min_size=99)
    staged_text = text + "\ninsert_job: q\njob_type: c\ncommand: x\n"

    async def scenario() -> None:
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        before = len(_watch_lines(run_root))
        engine.crash_point = _crash_at("after_sidecar")  # type: ignore[method-assign]
        staged = _stage(run_root, staged_text, name="fw.jil")
        assert "crash at after_sidecar" in await _refused(engine, _request(engine, staged))
        assert engine.barrier.parked is False
        await engine.run_until_quiescent(T0 + timedelta(seconds=130))
        assert len(_watch_lines(run_root)) > before

    asyncio.run(scenario())
    _close(engine)


def test_pr03_the_fw_append_re_proves_the_anchor_fence(tmp_path: Path) -> None:
    """PR-03's FW clause: the anchor is replaced between the observation and
    the line, and the append does not happen.

    The fence lives in the journal writer, and `watch.jsonl` is a spool the
    journal never sees -- so an append after leadership was lost would be
    evidence written by a non-leader."""
    run_root = tmp_path / "run"
    watch_file = tmp_path / "watched"
    watch_file.write_text("xxxxxxxx")
    engine, _ = _fw_estate(run_root, watch_file, min_size=99)

    async def scenario() -> None:
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        before = len(_watch_lines(run_root))
        (default_anchor_dir(run_root) / "anchor.lock").unlink()
        with pytest.raises(EngineError, match="can no longer prove it leads"):
            await engine.run_until_quiescent(T0 + timedelta(seconds=130))
        assert len(_watch_lines(run_root)) == before  # the observation, never the line

    asyncio.run(scenario())
    _close(engine)


# ------------------------------------------------- ss6 the cutoff proper


def test_pr25_c1_owns_every_tick_at_or_before_t(tmp_path: Path) -> None:
    """ss6 steps 4-5: the cutoff admits every scheduler tick due at or
    before T, and `scheduler_admitted_through: T` is the only carried
    evidence -- C1 owns every tick <= T, C2 owns every tick after it."""
    run_root = tmp_path / "run"
    catalog, sources = _catalog(SCHED_C1)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    at = datetime(2026, 7, 1, 9, 30)
    engine = start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=at),
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=_scheduler(SCHED_C1, at),
        staged=staged,
    )
    # the 09:00 tick of the NEXT day is not due; the cutoff admits nothing
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, SCHED_C2))))
    _close(engine)
    assert boundary.seal.scheduler_admitted_through == boundary.seal.closed_at
    assert boundary.seal.state.now == boundary.seal.closed_at


def test_pr25a_the_scheduler_frontier_is_semantic_not_the_newest_stamp(
    tmp_path: Path,
) -> None:
    """PR-25a: `leader` and `dispatch` records do NOT move the scheduler's
    durable frontier.

    T is 02:00, C2 opens at 02:10 and appends `leader.at = 02:10`, the
    process dies before the missed-tick sweep, and a frontier taken from
    the newest stamp anchors at 02:10 -- a 02:05 tick neither admitted nor
    recorded as dropped."""
    from dsl41.runner_journal import last_journal_at, scheduler_frontier

    opening = {
        "rec": "segment",
        "at": "2026-07-01T02:00:00",
        "first_index": 1,
        "period_id": 1,
        "segment_no": 1,
    }
    records = [
        opening,
        {"rec": "leader", "at": "2026-07-01T02:10:00", "epoch": 2},
        {"rec": "dispatch", "at": "2026-07-01T02:20:00"},
    ]
    assert scheduler_frontier(records) == datetime(2026, 7, 1, 2, 0)
    assert last_journal_at(records) == datetime(2026, 7, 1, 2, 20)
    records.append(
        {"rec": "input", "source": "scheduler", "at": "2026-07-01T02:05:00", "kind": "STARTJOB"}
    )
    assert scheduler_frontier(records) == datetime(2026, 7, 1, 2, 5)
    records.append({"rec": "drop", "at": "2026-07-01T02:07:00"})
    assert scheduler_frontier(records) == datetime(2026, 7, 1, 2, 7)


# ------------------------------------------------ ss11 seal selection


def test_pr46_an_orphan_sidecar_is_never_selected(tmp_path: Path) -> None:
    """ss11 step 3: a sidecar newer than the last committed record is an
    orphan -- no record names it, recovery ignores it, the period is still
    open."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    _close(engine)
    records = read_journal(active_wal(run_root))
    # a sidecar with nothing naming it: the crash between ss3's writes 1 and 2
    seal_path(run_root, 1).parent.mkdir(parents=True, exist_ok=True)
    seal_path(run_root, 1).write_text("{}")
    lineage = select_seal(run_root, records)
    assert lineage.seal is None and lineage.opens_next is False


def test_a_committed_seal_with_a_missing_sidecar_refuses(tmp_path: Path) -> None:
    """ss11's matrix row: committed `seal`, sidecar MISSING -- refuse, the
    boundary is unrecoverable."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    seal_path(run_root, 1).unlink()
    with pytest.raises(EngineError, match="unrecoverable"):
        select_seal(run_root, read_journal(active_wal(run_root)))


def test_a_sidecar_the_record_does_not_name_refuses(tmp_path: Path) -> None:
    """ss11 step 3: the digest AND every duplicated field are verified. A
    matching digest proves integrity, never derivation."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    records = read_journal(active_wal(run_root))
    records[-1] = {**records[-1], "digest": "sha256:" + "b" * 64}
    with pytest.raises(EngineError, match="disagrees with the .seal. record"):
        select_seal(run_root, records)


def test_the_staging_directory_is_where_the_digest_says(tmp_path: Path) -> None:
    """ss7's staging: `periods/.staging/<stage_digest>/` holds exactly the
    two files a client may write, and `candidate.json` binds the identity
    the rename drops from the path."""
    run_root = tmp_path / "run"
    _close(_genesis(run_root))
    staged = _stage(run_root, C2_JIL)
    directory = staging_dir(run_root, staged.stage_digest)
    assert sorted(p.name for p in directory.iterdir()) == [
        "candidate.json",
        "staged_manifest.json",
    ]
    candidate = read_candidate(directory)
    assert candidate is not None and candidate.next_period == staged


def test_a_candidate_missing_its_version_refuses_by_name(tmp_path: Path) -> None:
    """DL-157: the candidate is a closed artifact too (protocol-evolution's
    matrix), so an absent `artifact_format_version` refuses by name exactly
    as an unimplemented one does -- and still refuses an unimplemented one,
    unchanged. `next_period`'s own `artifact_format_version` is a different
    field on a different model and is untouched by this fix."""
    from dsl41.canon import ARTIFACT_FORMAT_VERSION
    from dsl41.period import CANDIDATE_NAME, CATALOG_HASH_VERSION

    directory = tmp_path / "staging"
    directory.mkdir()
    next_period = {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "catalog_hash": "sha256:" + "1" * 64,
        "catalog_hash_version": CATALOG_HASH_VERSION,
        "source_bundle_hash": "sha256:" + "2" * 64,
        "runtime_hash": "sha256:" + "3" * 64,
        "state_machine_version": STATE_MACHINE_VERSION,
    }
    body = {"stage_digest": "sha256:" + "4" * 64, "next_period": next_period}

    def _write(doc: dict[str, Any]) -> None:
        (directory / CANDIDATE_NAME).write_bytes(json.dumps(doc, sort_keys=True).encode() + b"\n")

    _write(body)
    with pytest.raises(EngineError, match="missing artifact_format_version"):
        read_candidate(directory)

    _write({**body, "artifact_format_version": ARTIFACT_FORMAT_VERSION})
    candidate = read_candidate(directory)
    assert candidate is not None and candidate.stage_digest == body["stage_digest"]

    _write({**body, "artifact_format_version": ARTIFACT_FORMAT_VERSION + 1})
    with pytest.raises(EngineError, match="not a Candidate this binary can read") as unsup:
        read_candidate(directory)
    assert "missing artifact_format_version" not in str(unsup.value)


def test_a_staged_manifest_missing_its_version_refuses_by_name(tmp_path: Path) -> None:
    """DL-157: `staged_manifest.json` shares the closed-artifact row with
    the other four, and was found to share their bug too, during this
    slice's own verification -- an absent `artifact_format_version`
    refuses by name exactly as an unimplemented one does, and still
    refuses an unimplemented one, unchanged."""
    from dsl41.canon import ARTIFACT_FORMAT_VERSION
    from dsl41.boundary import read_staged_manifest
    from dsl41.period import CATALOG_HASH_VERSION, STAGED_MANIFEST_NAME, runtime_hash

    directory = tmp_path / "staging2"
    directory.mkdir()
    profile = RuntimeProfile()
    body = {
        "catalog_hash": "sha256:" + "1" * 64,
        "catalog_hash_version": CATALOG_HASH_VERSION,
        "source_bundle_hash": "sha256:" + "2" * 64,
        "runtime_profile": profile.model_dump(mode="json"),
        "runtime_hash": runtime_hash(profile),
        "state_machine_version": STATE_MACHINE_VERSION,
    }

    def _write_staged(doc: dict[str, Any]) -> None:
        (directory / STAGED_MANIFEST_NAME).write_bytes(
            json.dumps(doc, sort_keys=True).encode() + b"\n"
        )

    _write_staged(body)
    with pytest.raises(EngineError, match="missing artifact_format_version"):
        read_staged_manifest(directory)

    _write_staged({**body, "artifact_format_version": ARTIFACT_FORMAT_VERSION})
    manifest = read_staged_manifest(directory)
    assert manifest is not None and manifest.catalog_hash == body["catalog_hash"]

    _write_staged({**body, "artifact_format_version": ARTIFACT_FORMAT_VERSION + 1})
    with pytest.raises(EngineError, match="not a StagedManifest this binary can read") as unsup:
        read_staged_manifest(directory)
    assert "missing artifact_format_version" not in str(unsup.value)


# ---------------------------------- DL-168: read strict from bytes, everywhere


def _dl168_sentinel_case(tmp_path: Path) -> None:
    """Caught by `canon.decode`'s own gate, ahead of any model -- the
    sentinel's only int field is `artifact_format_version` itself."""
    from dsl41.period import sentinel_path

    root = tmp_path / "sentinel"
    root.mkdir()
    sentinel_path(root).write_bytes(
        json.dumps(
            {
                "rec": "period_root",
                "artifact_format_version": True,
                "estate_id": "e",
                "see": "wal/",
            },
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    with pytest.raises(EngineError, match="artifact_format_version"):
        read_sentinel(root)


def _dl168_staged_manifest_case(tmp_path: Path) -> None:
    """`_read_artifact`'s `model_validate_json(raw, strict=True)` -- this
    entry's own fix, from a real staging directory."""
    from dsl41.boundary import read_staged_manifest
    from dsl41.period import STAGED_MANIFEST_NAME

    run_root = tmp_path / "run"
    _close(_genesis(run_root))
    staged = _stage(run_root, C2_JIL)
    directory = staging_dir(run_root, staged.stage_digest)
    path = directory / STAGED_MANIFEST_NAME
    payload = json.loads(path.read_bytes())
    payload["state_machine_version"] = True
    path.write_bytes(json.dumps(payload, sort_keys=True).encode() + b"\n")
    with pytest.raises(EngineError, match="state_machine_version"):
        read_staged_manifest(directory)


def _dl168_candidate_case(tmp_path: Path) -> None:
    """The same fix, on the NESTED field: `next_period.
    state_machine_version` proves the call-time `strict=True` cascades
    into it, which `Candidate`'s own `strict=True` config alone would not
    (protocol-evolution.md's DL-168 caveat -- that belt covers only
    `Candidate`'s own top-level fields)."""
    from dsl41.period import CANDIDATE_NAME

    run_root = tmp_path / "run"
    _close(_genesis(run_root))
    staged = _stage(run_root, C2_JIL)
    directory = staging_dir(run_root, staged.stage_digest)
    path = directory / CANDIDATE_NAME
    payload = json.loads(path.read_bytes())
    payload["next_period"]["state_machine_version"] = True
    path.write_bytes(json.dumps(payload, sort_keys=True).encode() + b"\n")
    with pytest.raises(EngineError, match="state_machine_version"):
        read_candidate(directory)


def _dl168_manifest_case(tmp_path: Path) -> None:
    """The pre-existing precedent this entry followed:
    `read_period_manifest`'s own `model_validate_json(raw, strict=True)`."""
    from dsl41.ir import lower_source
    from dsl41.period import genesis_manifest, write_period_manifest

    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\n")
    manifest = genesis_manifest(
        catalog, clock_domain="virtual", state_machine_version=STATE_MACHINE_VERSION
    )
    path = write_period_manifest(tmp_path, manifest)
    payload = json.loads(path.read_bytes())
    payload["catalog_hash_version"] = True
    path.write_bytes(json.dumps(payload, sort_keys=True).encode() + b"\n")
    with pytest.raises(EngineError, match="catalog_hash_version"):
        read_period_manifest(tmp_path)


def _dl168_attestation_case(tmp_path: Path) -> None:
    """The pre-existing `strict=True` model config: refuses at
    `model_validate`, ahead of the digest comparison (`attest.py`'s
    `from_bytes` validates the shape before it compares digests)."""
    from dsl41.canon import canonical_bytes

    attestation = Attestation(
        seal_digest="sha256:" + "1" * 64,
        period_id=1,
        chain_through_period=1,
        prev_attestation_digest=None,
        state_machine_version=STATE_MACHINE_VERSION,
        dsl41_version="1.2.3",
        audited_at="2026-08-25T00:00:00.000000",
    )
    payload = json.loads(attestation.to_bytes())
    payload["chain_through_period"] = True
    with pytest.raises(EngineError, match="valid integer"):
        Attestation.from_bytes(canonical_bytes(payload), where="laundered")


def _dl168_archive_receipt_case(tmp_path: Path) -> None:
    """The same pre-existing mechanism, on `ArchiveReceipt`."""
    from dsl41.canon import canonical_bytes
    from dsl41.period import ArchiveReceipt, archivable_names

    wal, _candidate_name, _staged_name = archivable_names(1)
    receipt = ArchiveReceipt(
        estate_id="e",
        period_id=1,
        seal_digest="sha256:" + "2" * 64,
        attestation_digest="sha256:" + "3" * 64,
        chain_through_period=1,
        retention_class="floor",
        archived=(wal,),
        archived_at="2026-08-25T00:00:00.000000",
        dsl41_version="1.2.3",
    )
    payload = json.loads(receipt.to_bytes())
    payload["chain_through_period"] = True
    with pytest.raises(EngineError, match="valid integer"):
        ArchiveReceipt.from_bytes(canonical_bytes(payload), where="laundered")


def _dl168_seal_sidecar_case(tmp_path: Path) -> None:
    """A fourth, independent mechanism: the self-describing digest is
    recomputed over the RAW payload before a model is even built
    (`seal.py`'s `from_payload`), so it refuses a laundered field with no
    field strictness required at all.

    The fixture MUST be written in ss3.2 canonical bytes, not
    `json.dumps`'s default (non-canonical) separators: non-canonical bytes
    refuse on the canonical-form gate regardless of whether `epoch` was
    laundered, which would prove nothing about this mechanism (a review
    finding on the first draft of this case)."""
    from dsl41.canon import canonical_bytes
    from dsl41.seal import Seal
    from test_seal_artifact import GOLDEN_BYTES

    payload = json.loads(GOLDEN_BYTES)
    payload["epoch"] = True
    with pytest.raises(EngineError, match="but the document hashes to"):
        Seal.from_bytes(canonical_bytes(payload))


_DL168_CASES = [
    pytest.param(_dl168_sentinel_case, id="sentinel"),
    pytest.param(_dl168_staged_manifest_case, id="staged_manifest"),
    pytest.param(_dl168_candidate_case, id="candidate"),
    pytest.param(_dl168_manifest_case, id="manifest"),
    pytest.param(_dl168_attestation_case, id="attestation"),
    pytest.param(_dl168_archive_receipt_case, id="archive_receipt"),
    pytest.param(_dl168_seal_sidecar_case, id="seal_sidecar"),
]


@pytest.mark.parametrize("case", _DL168_CASES)
def test_dl168_every_closed_artifact_reader_refuses_a_laundered_int_field(
    case: Any, tmp_path: Path
) -> None:
    """DL-168: a payload that launders one int field -- `true` where an
    int belongs -- refuses at every closed-artifact reader in scope, read
    from a valid artifact each case builds itself. One case per reader
    (genuinely parametrized: a regression in one case does not hide the
    other six, unlike this test's first draft, which ran all seven
    sequentially in one function and was wrongly called "parametrized" in
    the DL entry -- a review finding, corrected there and here).

    Anchor and claim are deliberately NOT here: both still launder (see
    the two `xfail` cases below) and are out of DL-168's scope by the
    brief's own ruling -- reported, not fixed."""
    case(tmp_path)


@pytest.mark.xfail(strict=True, reason="DL-168: EstateAnchor.read() is lax -- reported, not fixed")
def test_dl168_an_anchor_still_launders_a_nested_int_field(tmp_path: Path) -> None:
    """DL-168's ruling covers `_read_artifact` (staged manifest, candidate)
    and `StagedNextPeriod`. `EstateAnchor.read()` was out of that scope and
    was not touched: it still calls `Anchor.model_validate(payload)` lax,
    and `head.period_id` (`Field(ge=1)`) accepts a laundered `true` --
    coerced to `1`, which satisfies the floor, so nothing downstream
    notices. Verified against the real class before this test was written
    (CLAUDE.md: fidelity is tested, not asserted). This `xfail` is the
    citable record of the finding; it should start passing, and then be
    promoted to a real assertion, the day Anchor gets the same fix.

    `match="period_id"` (a review finding): a bare `pytest.raises(
    EngineError)` would XPASS -- and read as "Anchor got fixed" -- on ANY
    future `EngineError` `.read()` happens to raise, laundering-related or
    not. Matching the field name pins the xfail to the actual mechanism a
    real fix would refuse through."""
    from dsl41.boundary import ANCHOR_NAME
    from dsl41.canon import ARTIFACT_FORMAT_VERSION

    anchor_dir = tmp_path / "anchor"
    anchor_dir.mkdir()
    (anchor_dir / ANCHOR_NAME).write_bytes(
        json.dumps(
            {
                "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                "estate_id": "e",
                "head": {"state": "open", "period_id": True, "root": str(tmp_path / "r")},
                "periods": {},
            },
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    with pytest.raises(EngineError, match="period_id"):
        EstateAnchor(anchor_dir).read()


@pytest.mark.xfail(
    strict=True, reason="DL-168: EstateAnchor.read_claim() is lax -- reported, not fixed"
)
def test_dl168_a_claim_still_launders_its_int_field(tmp_path: Path) -> None:
    """The mirror finding on `read_claim`. `next_period` (`Field(ge=2)`) is
    Claim's only int field, and its floor happens to catch a `true`
    coercion by accident (`True` coerces to `1`, and `1 < 2` refuses on the
    bound regardless of strictness) -- so `true` does not discriminate
    here. A numeric string does: `"3"` coerces to `3` under a lax reader,
    `3 >= 2` passes the bound, and nothing refuses it. `read_claim` still
    calls `Claim.model_validate(payload)` lax, same as `EstateAnchor.read()`
    above. `match="next_period"` for the same reason as the anchor case."""
    from dsl41.boundary import CLAIMS_DIR
    from dsl41.canon import ARTIFACT_FORMAT_VERSION

    anchor_dir = tmp_path / "anchor"
    anchor = EstateAnchor(anchor_dir)
    claims_dir = anchor_dir / CLAIMS_DIR
    claims_dir.mkdir(parents=True)
    (claims_dir / "c1.json").write_bytes(
        json.dumps(
            {
                "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                "claim_id": "c1",
                "estate_id": "e1",
                "prev_seal_digest": "sha256:" + "a" * 64,
                "next_period": "3",
                "target_root": str(anchor_dir / "r"),
                "claimed_at": "2026-08-24T00:00:00.000000",
                "diag": {},
            },
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    with pytest.raises(EngineError, match="next_period"):
        anchor.read_claim("c1")


# ------------------------------- DL-170: SealRequest itself is strict too


def test_dl170_a_seal_request_refuses_a_coerced_wire_type_directly(tmp_path: Path) -> None:
    """DL-170: `SealRequest` gained `strict=True` in its own config, so the
    MODEL refuses a coerced wire type on construction, independent of
    whatever a caller's wire gate does or does not check ahead of it.
    `bool` is `int`'s subclass; under the old lax config `epoch=True`
    would have coerced to `1` silently, which is the exact hole DL-151
    named for a retry that could then match a committed seal under a type
    the original request never carried.

    A review finding on this entry, one commit before it: `pytest.raises
    (ValidationError)` alone does not say WHICH field or WHICH rule
    refused -- `test_dl168_a_seal_reads_its_next_period_strict_too` made
    the same correction for the nested model's own test. `errors()[0]`
    pins both: the offending field is `epoch`, and the reason is strict
    mode's own `int_type`, not `min_length` or a missing key."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)
    with pytest.raises(ValidationError) as excinfo:
        _request(engine, staged, epoch=True)
    error = excinfo.value.errors()[0]
    assert error["loc"] == ("epoch",)
    assert error["type"] == "int_type"
    _close(engine)


# ------------------------------------- DL-171: target_period is structural


def test_dl171_a_lineage_that_opens_next_without_a_seal_is_unconstructable() -> None:
    """DL-171: `select_seal` sets `opens_next=True` only on the branch that
    reads a seal first (`boundary.py`), so `opens_next` without a `seal` to
    open is a `Lineage` no caller of `select_seal` can build. The
    combination `cli_common.resume_target_period` and `runner_startup` each
    used to guard against by hand is now refused at construction, in
    `Lineage.__post_init__`."""
    with pytest.raises(AssertionError, match="opens_next requires seal"):
        Lineage(seal=None, opens_next=True)


def test_dl171_target_period_reads_the_seals_next_period_when_opens_next(
    tmp_path: Path,
) -> None:
    """`Lineage.target_period` (period-model ss11 step 5): the seal's own
    `next_period.period_id` when `opens_next`, regardless of the
    `fallback` passed in -- the half of `resume_target_period` and
    `runner_startup`'s duplicated guard that now lives in one place."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)
    asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)
    lineage = select_seal(run_root, read_journal(active_wal(run_root)))
    assert lineage.opens_next is True
    assert lineage.seal is not None
    assert lineage.target_period(999) == lineage.seal.next_period.period_id


def test_dl171_target_period_falls_back_when_it_does_not_open_next() -> None:
    """The other half: no committed boundary to open, so `target_period`
    hands back exactly the `fallback` it was given, untouched."""
    lineage = Lineage(seal=None, opens_next=False)
    assert lineage.target_period(7) == 7


# ------------------------------------------- ss2.2 the `seal` control verb


@pytest.fixture
def short_root():
    """A short-path base for AF_UNIX control sockets (test_runner_control's
    fixture, for the same reason: `sun_path` is 104 bytes on macOS)."""
    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="dsl41b-"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _seal_request_wire(engine, staged: StagedNextPeriod, **overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "cmd": "seal",
        "v": 3,
        "baseline_id": engine.baseline_id,
        "epoch": engine.epoch,
        "request_id": "r-wire-1",
        "next_period": staged.model_dump(mode="json"),
        "stage_digest": staged.stage_digest,
        "force_seal": False,
        "claimed_actor": "alice@ops",
    }
    return {**request, **overrides}


def test_pr30a_the_seal_verb_answers_before_the_engine_exits(short_root: Path) -> None:
    """ss2.2's v3 verb, end to end over the socket: the boundary commits,
    the client is answered with the seal's identity, and the engine exits
    `PeriodSealed` -- which `dsl41 run` spends code 3 on (PR-30b)."""
    from dsl41.runner_clock import RealClock
    from dsl41.runner_control import ControlClient, ControlServer

    run_root = short_root / "run"
    catalog, sources = _catalog(C1_JIL)
    staged_manifest = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    engine = start_run(
        catalog,
        run_root,
        clock=RealClock(),
        adapters={"CMD": FakeAdapter(default=None)},
        hold_open=True,
        staged=staged_manifest,
    )

    async def scenario() -> dict[str, Any]:
        server = ControlServer(engine, run_root / "control.sock")
        await server.start()
        loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        client = ControlClient(run_root / "control.sock")
        try:
            staged = _stage(run_root, C2_JIL)
            answer = await client.request(_seal_request_wire(engine, staged))
            with pytest.raises(PeriodSealed):
                await loop_task
            return answer
        finally:
            await client.close()
            await server.close()
            await engine.shutdown()

    answer = asyncio.run(scenario())
    _close(engine)
    assert answer["ok"] is True and answer["kind"] == "seal"
    assert answer["decision"] == "applied" and answer["period_id"] == 1
    assert answer["next_period_id"] == 2
    assert answer["digest"] == read_seal(run_root, 1).digest
    assert answer["baseline_id"] == engine.baseline_id  # the ss6 read header


def test_a_seal_request_names_an_expect_on_nothing(tmp_path: Path) -> None:
    """ss2.2: `expect` is absent BY DESIGN -- a seal addresses no row --
    and "no row" must therefore mean `expect` is refused, not optional."""
    from dsl41.runner_admission import EnvelopeError, parse_envelope

    request = {"v": 3, "baseline_id": "b", "epoch": 1, "request_id": "r"}
    envelope = parse_envelope(request, addressed=None, baseline_id="b")
    assert envelope.expect == {}
    with pytest.raises(EnvelopeError, match="addresses no row"):
        parse_envelope({**request, "expect": {"job:a": 1}}, addressed=None, baseline_id="b")
    # the KEY is what arrives: a null `expect` is an expect (DL-151). The
    # control server asked this by key and the envelope by value, so a
    # `"expect": null` that the server answered first was refused and one
    # that reached any other envelope reader was not
    with pytest.raises(EnvelopeError, match="addresses no row"):
        parse_envelope({**request, "expect": None}, addressed=None, baseline_id="b")


def test_pr30e_a_committed_seals_exact_retry_is_answered_from_the_new_period(
    tmp_path: Path,
) -> None:
    """ss2.2's retry route, ahead of the baseline gate: the engine of period
    N+1 keeps the `seal` record it opened from and answers an exact retry
    from it -- a retry of the boundary that closed C1 necessarily carries
    B1 while C2 answers under B2."""
    from dsl41.runner_control import ControlServer

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)
    boundary = asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)
    opened = _resume(run_root, C2_JIL)
    server = ControlServer(opened, run_root / "control.sock")

    retry = _request(engine, staged)  # composed under C1's baseline, as a retry is
    wire = {
        "cmd": "seal",
        "v": 3,
        "baseline_id": retry.baseline_id,
        "epoch": retry.epoch,
        "request_id": retry.request_id,
        "next_period": staged.model_dump(mode="json"),
        "stage_digest": staged.stage_digest,
        "force_seal": False,
        "claimed_actor": "alice@ops",
    }
    answer = asyncio.run(server._seal(wire))
    assert answer["ok"] is True and answer["decision"] == "applied"
    assert answer["digest"] == boundary.seal.digest
    assert answer["next_period_id"] == 2
    # PR-30c: one `request_id`, a different envelope -> a collision, refused
    collision = asyncio.run(server._seal({**wire, "force_seal": True}))
    assert collision["refused"] is True and "under a retry" in collision["error"]
    _close(opened)


def test_a_seal_request_under_a_foreign_baseline_is_refused(tmp_path: Path) -> None:
    """The generic v3 gate still applies to everything the retry route does
    not answer: a request composed against another baseline names nothing
    here."""
    from dsl41.runner_control import ControlServer

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    server = ControlServer(engine, run_root / "control.sock")
    staged = _stage(run_root, C2_JIL)
    answer = asyncio.run(server._seal(_seal_request_wire(engine, staged, baseline_id="other")))
    assert answer["refused"] is True and "baseline_id" in answer["error"]
    _close(engine)


# ----------------------------------------- the corners each rule owns alone


def test_one_seal_at_a_time(tmp_path: Path) -> None:
    """A second boundary while one is in flight is refused, not queued: the
    cutoff is the one act that must observe a state nothing else moves."""
    from dsl41.runner_admission import AdmissionRefused

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)

    async def scenario() -> None:
        first = engine.submit_seal(_request(engine, staged))
        second = engine.submit_seal(_request(engine, staged, request_id="r-2"))
        with pytest.raises(AdmissionRefused, match="one seal at a time"):
            await second
        first.cancel()

    asyncio.run(scenario())
    _close(engine)


def test_a_client_that_walked_away_does_not_stop_the_boundary(tmp_path: Path) -> None:
    """The awaitable is the client's; the boundary is the estate's. A
    cancelled wait -- the control server's `wait_for` timing out -- neither
    commits nor refuses anything on its own."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)

    async def committed() -> None:
        future = engine.submit_seal(_request(engine, staged))
        future.cancel()
        with pytest.raises(PeriodSealed):
            await engine.run_until_quiescent(datetime.max)

    asyncio.run(committed())
    _close(engine)
    assert read_seal(run_root, 1).period_id == 1

    other = tmp_path / "run2"
    engine2 = _genesis(other)
    engine2.crash_point = _crash_at("after_sidecar")  # type: ignore[method-assign]
    staged2 = _stage(other, C2_JIL)

    async def refused() -> None:
        future = engine2.submit_seal(_request(engine2, staged2))
        future.cancel()
        await engine2.run_until_quiescent(T0)
        assert engine2.sealing is False  # the abort still ran

    asyncio.run(refused())
    _close(engine2)


def test_an_engine_with_no_lineage_has_no_boundary_to_close(tmp_path: Path) -> None:
    """The bisimulation harness and a rehearsal lead no lineage; a seal over
    one has nothing to close."""
    from dsl41.runner_admission import AdmissionRefused

    catalog, _ = _catalog(C1_JIL)
    engine = Engine(catalog, clock=VirtualClock(start=T0), adapters={})
    staged = StagedNextPeriod(
        catalog_hash="sha256:" + "0" * 64,
        source_bundle_hash="sha256:" + "1" * 64,
        runtime_hash="sha256:" + "2" * 64,
        state_machine_version=STATE_MACHINE_VERSION,
    )

    async def scenario() -> None:
        future = engine.submit_seal(_request(engine, staged))
        await engine.run_until_quiescent(T0)
        with pytest.raises(AdmissionRefused, match="leads no lineage"):
            future.result()

    asyncio.run(scenario())


def test_a_seal_composed_against_a_superseded_leader_is_refused(tmp_path: Path) -> None:
    """The ss4 rule every other external input meets, at the one place a
    seal passes."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)
    message = asyncio.run(_refused(engine, _request(engine, staged, epoch=engine.epoch + 5)))
    assert "is not this leader's" in message
    _close(engine)


def test_a_digest_nothing_was_staged_under_is_refused(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)
    import shutil

    shutil.rmtree(staging_dir(run_root, staged.stage_digest))
    message = asyncio.run(_refused(engine, _request(engine, staged)))
    assert "nothing is staged at this digest" in message
    _close(engine)


def test_pr28e_an_attempt_admitted_just_before_the_cut_is_decided_first(
    tmp_path: Path,
) -> None:
    """ss6 step 2: every already-admitted attempt drains to its DURABLE
    decision before any sidecar byte is written."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)

    async def scenario() -> None:
        engine.inject(Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": "in"}))
        assert engine._queue  # queued, not yet admitted, when the barrier starts
        await _seal(engine, _request(engine, staged))

    asyncio.run(scenario())
    _close(engine)
    records = read_journal(wal_path(run_root, 1))
    kinds = [record["rec"] for record in records]
    assert kinds[-1] == "seal"
    decided = {r["index"] for r in records if r["rec"] == "decision"}
    admitted = {r["seq"] for r in records if r["rec"] in ("input", "advance")}
    assert admitted == decided  # nothing admitted-without-decision
    assert read_seal(run_root, 1).state.globals["G"].value == "in"


def test_pr25_the_cutoff_admits_a_tick_due_at_t(tmp_path: Path) -> None:
    """ss6 step 4: the cutoff admits every scheduler tick due at or before
    T -- C1 owns them, and `Scheduler._next` cannot be re-derived after a
    seal cuts the evidence away."""
    run_root = tmp_path / "run"
    catalog, sources = _catalog(SCHED_C1)
    staged_manifest = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    tick = datetime(2026, 7, 1, 9, 0)
    engine = start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=tick),  # T is the tick's own instant
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=_scheduler(SCHED_C1, T0),
        staged=staged_manifest,
    )
    staged = _stage(run_root, SCHED_C2)

    async def scenario():
        # the runbook's step 1 hold, still queued when the barrier freezes:
        # ss6 step 2 drains it, and the tick step 4 admits then LATCHES
        engine.inject(Event(at=tick, kind="ON_HOLD", payload={"job": "a"}))
        return await _seal(engine, _request(engine, staged))

    boundary = asyncio.run(scenario())
    _close(engine)
    ticks = [
        record
        for record in read_journal(wal_path(run_root, 1))
        if record.get("rec") == "input" and record.get("source") == "scheduler"
    ]
    assert [record["at"] for record in ticks] == [tick.isoformat()]
    assert boundary.seal.scheduler_admitted_through == tick
    # C1 owns the tick, and the hold turns it into a latch rather than a run
    assert boundary.seal.state.jobs["a"].armed is True
    assert boundary.seal.state.jobs["a"].run_number == 0


def test_every_quiescence_refusal_names_its_own_reason(tmp_path: Path) -> None:
    """ss8's "always" set, one reason at a time. Called directly: each of
    these is a state the drain above normally clears, and the point is that
    a seal REFUSES rather than snapshots any one of them."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    estate = engine.estate
    assert estate is not None
    assert engine._not_quiescent(estate) is None

    engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "a"}))
    assert "input(s) still queued" in str(engine._not_quiescent(estate))
    engine._queue.clear()

    from dsl41.runner_admission import Frontiers

    engine.frontiers = Frontiers(committed_index=4, applied_index=3, at=T0)
    assert "admitted and undecided" in str(engine._not_quiescent(estate))
    engine.frontiers = Frontiers(committed_index=4, applied_index=4, at=T0)

    engine._reaping.append(_never_done())  # type: ignore[arg-type]
    assert "KILL ladder(s) have not resolved" in str(engine._not_quiescent(estate))
    engine._reaping.clear()

    _start_a(engine)
    from dsl41.runner_effects import EffectOutcome

    effect = next(e for e in engine.outbox.effects() if e.kind == "SPAWN")
    engine.outbox.resolve(
        EffectOutcome(effect_id=effect.effect_id, state="indeterminate", run_id=effect.run_id)
    )
    assert "indeterminate effect(s)" in str(engine._not_quiescent(estate))
    _close(engine)


def _never_done() -> Any:
    """A stand-in for a KILL ladder that has not resolved."""

    class _Pending:
        def done(self) -> bool:
            return False

    return _Pending()


def test_the_journal_writes_seal_records_and_nothing_else_through_that_door(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    assert engine.journal is not None
    with pytest.raises(EngineError, match="writes `seal` records"):
        engine.journal.seal({"rec": "drop", "at": T0.isoformat()})
    _close(engine)


def test_pr16c_a_held_spawn_crosses_the_boundary_as_a_pending_intent(
    tmp_path: Path,
) -> None:
    """ss3.3: `outbox_pending` is intents recorded and not delivered, and it
    is carried. A SPAWN born for a host that routes nothing is held by the
    routing gate; the next period inherits the intent, not a lost start."""
    from dsl41.runner_hosts import HostCommand

    run_root = tmp_path / "run"
    engine = _genesis(run_root)

    async def under_c1() -> None:
        engine.inject_host(HostCommand(verb="drain", host_id=engine.executor_id))
        await engine.run_until_quiescent(T0)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "a"}))
        await engine.run_until_quiescent(T0)

    asyncio.run(under_c1())
    held = [e for e in engine.outbox.pending() if e.kind == "SPAWN"]
    assert len(held) == 1
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    assert [e.effect_id for e in boundary.seal.outbox_pending] == [held[0].effect_id]
    assert boundary.seal.executions[0].kind == "pending_spawn"

    opened = _resume(run_root, C2_JIL)
    assert [e.effect_id for e in opened.outbox.pending()] == [held[0].effect_id]
    _close(opened)


def test_an_opening_whose_manifest_was_pruned_refuses(tmp_path: Path) -> None:
    """The retention floor forbids pruning what the head reaches, and
    recovery cannot invent a manifest the boundary renamed into place."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    (period_dir(run_root, 2) / "manifest.json").unlink()
    with pytest.raises(EngineError, match="may never be pruned"):
        _resume(run_root, C2_JIL)


def test_pr33_an_orphan_on_an_unsupervised_adapter_is_left_alone(tmp_path: Path) -> None:
    """PR-33's negative half: the re-drive is the SUPERVISED path's. A
    tethered adapter's wrapper died with the engine, so there is nothing
    for this rung to signal."""
    from dsl41.runner_startup import _redrive_orphans

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    _start_a(engine)
    engine.inject(
        Event(at=T0, kind="STATUS", payload={"job": "a", "status": "SUCCESS", "exit_code": 0})
    )
    asyncio.run(engine.run_until_quiescent(T0))
    assert engine.oracle.store.runtime("a").status == "SUCCESS"
    listing = {("a", 1): {"wrapper_alive": True, "run_id": "not-a-supervised-run"}}
    asyncio.run(_redrive_orphans(engine, listing, set()))  # no adapter to signal: a no-op
    _close(engine)


def test_pr28b_a_fence_loss_inside_the_interval_fail_stops(tmp_path: Path) -> None:
    """PR-28b's last clause, and DL-101's rule: an abort reopens admission,
    and a leader that cannot prove it leads does not get to.

    It cannot un-run what happened; what it can do is turn a divergence
    into a recorded stop."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)

    def hook(name: str) -> None:
        if name == "after_committed_manifest":
            (default_anchor_dir(run_root) / "anchor.lock").unlink()
            raise EngineError("crash with the fence gone")

    engine.crash_point = hook  # type: ignore[method-assign]

    async def scenario() -> None:
        engine.submit_seal(_request(engine, staged))
        with pytest.raises(EngineError, match="crash with the fence gone"):
            await engine.run_until_quiescent(T0)

    asyncio.run(scenario())
    assert engine.sealing is True  # admission stays closed: no abort ran
    _close(engine)


def test_an_estate_that_never_settles_refuses_rather_than_hangs(tmp_path: Path) -> None:
    """ss6 step 2's drain is bounded: a boundary over a moving state is not
    a boundary, and a wedged tier must refuse rather than hang."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    engine.QUIESCE_WAIT_S = -1.0  # every drain is already past its deadline
    staged = _stage(run_root, C2_JIL)

    async def scenario() -> None:
        engine.inject(Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": "1"}))
        message = await _refused(engine, _request(engine, staged))
        assert "the estate is not settling" in message

    asyncio.run(scenario())
    _close(engine)


def test_carried_rows_install_only_into_a_pristine_state(tmp_path: Path) -> None:
    """ss7 phase 3 step 3: carried rows are ASSEMBLY's first act. A live
    state advances through its own inputs alone -- installing over one
    would replace rows an input already moved, with no record of either."""
    from dsl41.ir import lower_source
    from dsl41.oracle import Oracle
    from dsl41.oracle_state import CarriedRows

    store = Oracle(lower_source("insert_job: j\njob_type: c\ncommand: x\n")).store
    store.begin_input()
    store.set_global("G", "1")
    store.commit_input()
    with pytest.raises(ValueError, match="install on a used state"):
        store.install(CarriedRows(period_id=2))


def test_pr03_a_read_after_the_anchor_is_replaced_is_refused_not_answered(
    tmp_path: Path,
) -> None:
    """PR-03: the anchor is deleted under a live incumbent, and its next
    revision-bearing READ is refused -- not answered.

    Frozen v2 makes these reads leader-only and stamps lineage coordinates
    on each one, so a displaced leader that kept answering until its next
    mutation would be publishing revisions from a lineage it no longer
    leads."""
    from dsl41.runner_control import ControlServer

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    server = ControlServer(engine, run_root / "control.sock")
    answered = asyncio.run(server._respond({"cmd": "status", "v": 3}))
    assert answered["ok"] is True and "baseline_id" in answered

    (default_anchor_dir(run_root) / "anchor.lock").unlink()
    refused = asyncio.run(server._respond({"cmd": "status", "v": 3}))
    assert refused["refused"] is True
    assert "no longer prove it leads" in refused["error"]
    # and the read header is ABSENT: those are the coordinates this process
    # may no longer speak for
    assert "baseline_id" not in refused and "applied_index" not in refused

    class _Writer:
        def __init__(self) -> None:
            self.lines: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.lines.append(data)

        async def drain(self) -> None:
            return None

    writer = _Writer()
    asyncio.run(server._subscribe(writer, {"cmd": "subscribe", "v": 3}))  # type: ignore[arg-type]
    assert b"no longer prove it leads" in b"".join(writer.lines)
    # losing the RUN ROOT's proof is CM-14's rule and is NOT this door's:
    # refusing there would also refuse the read a client composes its
    # `expect` from, so the mutation that stops the engine would never be
    # sent. That half is `test_cm14_an_engine_that_cannot_prove_it_leads_
    # stops_dispatching`, over two real processes.
    (default_anchor_dir(run_root) / "anchor.lock").write_text("")
    restored = asyncio.run(server._respond({"cmd": "status", "v": 3}))
    assert restored["refused"] is True  # replaced, not merely deleted
    _close(engine)


def test_pr30d_alternating_candidates_quarantine_without_colliding(tmp_path: Path) -> None:
    """PR-30d: S1 -> S2 -> S1 -> S2, each retry superseding the installed
    candidate. Three supersessions, three quarantine paths, no collision,
    and the boundary commits on the identity the last request names.

    The path is two levels -- `<old stage digest>/<sha256 of its
    manifest.json>/` -- precisely because the same digest comes round
    again, and a one-level path would have to overwrite or refuse."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    engine.crash_point = _crash_at("after_install")  # type: ignore[method-assign]
    order = [C2_JIL, C1_JIL, C2_JIL, C1_JIL]
    digests: list[str] = []
    for text in order[:-1]:
        # a client STAGES before every attempt, as `dsl41 seal` does: the
        # bundle is content-addressed so the repeat is idempotent, and the
        # rename moved the previous staging directory into place
        staged = _stage(run_root, text)
        digests.append(staged.stage_digest)
        asyncio.run(_refused(engine, _request(engine, staged)))
        assert read_candidate(period_dir(run_root, 2)).stage_digest == staged.stage_digest  # type: ignore[union-attr]
        # each attempt admits an index, so the next one's `first_index` moves
        engine.inject(Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": "x"}))
        asyncio.run(engine.run_until_quiescent(T0))
    engine.crash_point = _no_crash  # type: ignore[method-assign]
    last = _stage(run_root, order[-1])
    boundary = asyncio.run(_seal(engine, _request(engine, last)))
    _close(engine)
    assert boundary.seal.next_period.stage_digest == last.stage_digest
    quarantined = sorted(
        read_candidate(path.parent).stage_digest  # type: ignore[union-attr]
        for path in (run_root / "periods" / ".quarantine").rglob("candidate.json")
    )
    assert quarantined == sorted(digests)  # three supersessions, three paths


def test_a_staging_directory_removed_under_the_barrier_refuses(tmp_path: Path) -> None:
    """The window between readiness and the install: readiness resolved
    these bytes, and by the time the barrier finished the staged directory
    was gone. Refused -- the alternative is renaming a directory that is
    not there."""
    import shutil

    from dsl41.boundary import _prepare_install, read_staged_manifest

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)
    directory = staging_dir(run_root, staged.stage_digest)
    bytes_ = read_staged_manifest(directory)
    assert bytes_ is not None
    committed = bytes_.commit(
        period_id=2,
        baseline_id="sha256:" + "0" * 64,
        clock_domain="virtual",
        segment_no=2,
        first_index=9,
    )
    shutil.rmtree(directory)
    with pytest.raises(EngineError, match="never staged"):
        _prepare_install(
            run_root,
            staged=staged,
            committed_manifest=committed,
            crash_point=lambda _stage: None,
        )
    _close(engine)


def test_pr30b_a_live_seal_exits_the_engine_with_code_three(short_root: Path) -> None:
    """PR-30b, over two real processes: the boundary is requested through
    the socket, the engine commits it and **exits with code 3**.

    Its own code, distinct from the 0 of an operator stop, the 1 of a crash
    and the 2 of a run that never started -- so an init system does not
    restart-loop a sealed engine. `run` treats an engine-loop return as
    failure code 1, which is why this exit path is its own obligation
    rather than a footnote."""
    import subprocess
    import sys
    import threading

    from dsl41.runner_control import roundtrip

    run_root = short_root / "run"
    estate = short_root / "estate.jil"
    estate.write_text(C1_JIL)
    proc = subprocess.Popen(
        [sys.executable, "-m", "dsl41", "run", "--run-root", str(run_root), str(estate)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        banner: list[str] = []
        reader = threading.Thread(target=lambda: banner.append(proc.stdout.readline()))  # type: ignore[union-attr]
        reader.daemon = True
        reader.start()
        reader.join(60.0)
        assert banner and banner[0].startswith("engine up;"), banner
        socket_path = run_root / "control.sock"
        header = roundtrip(socket_path, {"cmd": "status", "v": 3})
        staged = _stage(run_root, C2_JIL)  # exactly what `dsl41 seal` stages
        answer = roundtrip(
            socket_path,
            {
                "cmd": "seal",
                "v": 3,
                "baseline_id": header["baseline_id"],
                "epoch": header["epoch"],
                "request_id": "r-live-seal",
                "next_period": staged.model_dump(mode="json"),
                "stage_digest": staged.stage_digest,
                "force_seal": False,
                "claimed_actor": "alice@ops",
            },
            timeout=60.0,
        )
        assert answer["ok"] is True, answer
        assert answer["kind"] == "seal" and answer["next_period_id"] == 2
        assert proc.wait(timeout=60) == 3
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a failed assertion
            proc.kill()
            proc.wait()
    assert read_seal(run_root, 1).digest == answer["digest"]
    # and the estate is left ready to open, not half-closed
    anchor = EstateAnchor(default_anchor_dir(run_root))
    stored = anchor.read()
    assert stored is not None and isinstance(stored.head, ClosedHead)


def test_pr30_the_gate_reads_a_real_wal_not_a_synthetic_one(tmp_path: Path) -> None:
    """ss9 against the bytes the journal actually writes.

    An attempt carries its number under `seq` and a decision under
    `index` -- one is the subscribe cursor, the other is the attempt's own
    number (DL-89). A gate that joined on the wrong key would find nothing,
    pass every boundary and protect no retry, and every synthetic-record
    test would still be green. So this one drives a real external command
    into a real segment and seals two seconds later."""
    from dsl41.runner_admission import Envelope

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)

    async def scenario() -> str:
        decided = engine.submit(
            Event(at=T0, kind="ON_HOLD", payload={"job": "b"}),
            Envelope(request_id="r-ext", expect={"job:b": 1}, epoch=engine.epoch),
        )
        await engine.run_until_quiescent(T0)
        # a REJECTED attempt, and deliberately: "attempt", not "mutation" --
        # the frozen exact-retry promise covers a journaled rejection as
        # much as a state change, so a CAS loser holds the gate exactly as
        # an applied command would (ss3.1)
        assert (await decided).decision == "rejected"
        return await _refused(engine, _request(engine, staged))

    message = asyncio.run(scenario())
    assert "retry_horizon_us" in message  # the gate is engaged, not empty
    assert "--force-seal" in message
    # forced, it commits and RECORDS the override -- so the log alone shows
    # a forced boundary (ss3.1's truth table)
    boundary = asyncio.run(_seal(engine, _request(engine, staged, force_seal=True)))
    _close(engine)
    assert boundary.seal.boundary_request.force_seal is True
    gate = boundary.seal.forced_gate
    assert gate is not None and gate.gate == "retry_horizon"
    assert gate.horizon_us == round(RETRY_HORIZON_S * 1_000_000)
    assert boundary.record["force_seal"] is True


def test_pr50_run_history_reads_the_estate_after_a_boundary(tmp_path: Path) -> None:
    """PR-50's manifest half: `dsl41 runs` over a root that has crossed a
    boundary reads the ACTIVE period's manifest.

    Every artifact under `periods/` is addressed by the period number, so a
    reader that defaulted to period 1 after a seal would open period 1's
    manifest beside period 2's records and refuse the whole root as
    inconsistent -- the tool would break on exactly the estates the
    boundary exists for.

    PR-50's other half -- history SPANS the boundary, and a run stays under
    the period that dispatched it -- cannot be held here: this tier's
    adapter is a `FakeAdapter`, only a real adapter writes the `dispatch`
    record a leaf row is built from, and a fixture that produces no rows
    can say nothing about which rows survive. It is
    `test_pr50_run_history_spans_a_boundary` in tests/test_run_history.py,
    over real subprocesses. `assert isinstance(rows, list)` stood here
    instead, and it is what let a reader that opened the ACTIVE segment
    alone -- answering with an EMPTY table after every seal -- ship."""
    from dsl41.runner_history import read_run_root, stored_input_paths

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    _start_a(engine)
    engine.inject(
        Event(at=T0, kind="STATUS", payload={"job": "a", "status": "SUCCESS", "exit_code": 0})
    )
    asyncio.run(engine.run_until_quiescent(T0))
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    opened = _resume(run_root, C2_JIL)
    _close(opened)

    assert stored_input_paths(run_root)  # C2's bundle, not C1's
    rows = read_run_root(run_root)  # refuses nothing: this is period 2's root
    assert rows == []  # and nothing was dispatched: no adapter ever wrote one
    manifest = read_period_manifest(run_root, 2)
    assert manifest is not None
    assert read_journal(run_root)[0]["catalog_hash"] == manifest.catalog_hash


def test_pr25_the_next_period_starts_its_scheduler_strictly_after_t(tmp_path: Path) -> None:
    """period-model ss6: C1 owns every tick at or before T, C2 owns every
    tick after it, so the opening segment drops none of C1's.

    C1's cutoff admitted and ran every tick due at or before T -- that is
    what `scheduler_admitted_through` records. Resume anchors INCLUSIVELY at
    T and does re-derive the tick C1 just ran; the sweep's cutoff clause is
    what stops it becoming a `drop` saying the engine had missed it
    (DL-166). That record would be durably false, and it would move the ss6
    frontier and read to an operator as a lost tick."""
    run_root = tmp_path / "run"
    catalog, sources = _catalog(SCHED_C1)
    staged_manifest = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    tick = datetime(2026, 7, 1, 9, 0)
    engine = start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=tick),
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=_scheduler(SCHED_C1, T0),
        staged=staged_manifest,
    )

    async def under_c1():
        await engine.run_until_quiescent(tick)  # the tick fires and `a` starts
        engine.inject(
            Event(at=tick, kind="STATUS", payload={"job": "a", "status": "SUCCESS", "exit_code": 0})
        )
        await engine.run_until_quiescent(tick)
        return await _seal(engine, _request(engine, _stage(run_root, SCHED_C2)))

    boundary = asyncio.run(under_c1())
    _close(engine)
    assert boundary.seal.state.jobs["a"].run_number == 1  # C1 ran the tick at T
    assert boundary.seal.scheduler_admitted_through == tick

    catalog2, _ = _catalog(SCHED_C2)
    opened = asyncio.run(
        resume_run(
            catalog2,
            run_root,
            clock=VirtualClock(start=tick),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=_scheduler(SCHED_C2, T0),
        )
    )
    _close(opened)
    drops = [r for r in read_journal(wal_path(run_root, 2)) if r.get("rec") == "drop"]
    assert drops == []  # C1 owned that tick; C2 never missed it
    assert opened.drops == []


def test_dl166_a_tick_this_segment_admitted_after_t_is_skipped_without_a_drop(
    tmp_path: Path,
) -> None:
    """The resume sweep's OTHER dedup clause (DL-166): a tick this segment
    admitted itself, strictly after T.

    The period opens from a seal at T, runs on, journals the 09:00 tick, and
    the process dies. Resume re-anchors at the frontier -- 09:00, strictly
    after T -- so the inclusive anchor re-derives that tick. Here the cutoff
    clause is inert (09:00 > T) and the journal's own record is what skips
    it; a `drop` would say the engine missed a tick it in fact ran."""
    from dsl41.runner_journal import scheduler_frontier

    run_root = tmp_path / "run"
    catalog, sources = _catalog(SCHED_C1)
    staged_manifest = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    engine = start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=_scheduler(SCHED_C1, T0),
        staged=staged_manifest,
    )
    # sealed at T0, an hour BEFORE the 09:00 tick: C1 admitted none of it
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, SCHED_C2))))
    _close(engine)
    assert boundary.seal.scheduler_admitted_through == T0

    catalog2, _ = _catalog(SCHED_C2)
    tick = datetime(2026, 7, 1, 9, 0)
    opened = asyncio.run(
        resume_run(
            catalog2,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=_scheduler(SCHED_C2, T0),
        )
    )

    async def under_c2() -> None:
        await opened.run_until_quiescent(tick)  # C2 admits the tick and journals it
        opened.inject(
            Event(at=tick, kind="STATUS", payload={"job": "a", "status": "SUCCESS", "exit_code": 0})
        )
        await opened.run_until_quiescent(tick)

    asyncio.run(under_c2())
    _close(opened)  # the crash, one tick past the opening
    admitted = [
        record
        for record in read_journal(wal_path(run_root, 2))
        if record.get("rec") == "input" and record.get("source") == "scheduler"
    ]
    assert [record["at"] for record in admitted] == [tick.isoformat()]
    assert scheduler_frontier(read_journal(wal_path(run_root, 2))) == tick  # > T

    again = asyncio.run(
        resume_run(
            catalog2,
            run_root,
            clock=VirtualClock(start=tick),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=_scheduler(SCHED_C2, T0),
        )
    )
    _close(again)
    assert again.drops == []
    drops = [r for r in read_journal(wal_path(run_root, 2)) if r.get("rec") == "drop"]
    assert drops == []
    # nor re-admitted: the sweep only ever drops today, so this is the guard
    # on the other half of the risk -- one tick, fired once
    still = [
        record
        for record in read_journal(wal_path(run_root, 2))
        if record.get("rec") == "input" and record.get("source") == "scheduler"
    ]
    assert [record["at"] for record in still] == [tick.isoformat()]


@pytest.mark.parametrize("damage", ["empty", "torn"])
def test_pr45_a_segment_that_never_opened_is_re_opened_from_the_boundary(
    tmp_path: Path, damage: str
) -> None:
    """ss11's matrix row: a torn or empty FIRST line means the segment
    never opened -- re-open it from the boundary.

    Not "refuse": nothing was lost. The opening is a pure function of the
    seal, so what replaces the file is byte-identical (PR-07). And not
    "append": a second `segment` record in one file is the two-candidate
    state I1 exists to make impossible."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    first = _resume(run_root, C2_JIL)
    _close(first)
    intact = wal_path(run_root, 2).read_bytes()

    # the crash: the opening segment's first line never landed whole
    if damage == "empty":
        wal_path(run_root, 2).write_bytes(b"")
    else:
        wal_path(run_root, 2).write_bytes(intact[: len(intact) // 3])

    second = _resume(run_root, C2_JIL)
    _close(second)
    assert second.oracle.store.period_id == 2
    records = read_journal(wal_path(run_root, 2))
    assert [r["rec"] for r in records].count("segment") == 1  # never two
    assert wal_path(run_root, 2).read_bytes().splitlines()[0] == intact.splitlines()[0]


# ------------------------------------------- ss7 phase 1, check by check


def _staged_context(run_root: Path, engine, *, text: str = C2_JIL, **overrides: Any):
    """A phase-1 context over a real staged C2, so each case below injects
    exactly ONE failure into an otherwise valid one."""
    from dsl41.boundary import StagedContext, load_staged_catalog, read_staged_manifest
    from dsl41.classify import Baseline

    staged = _stage(run_root, text)
    bytes_ = read_staged_manifest(staging_dir(run_root, staged.stage_digest))
    assert bytes_ is not None
    estate = engine.estate
    assert estate is not None
    request = _request(engine, staged)
    fields: dict[str, Any] = {
        "staged": staged,
        "staged_bytes": bytes_,
        "boundary_request": request.boundary_request,
        "request_fingerprint": request.fingerprint,
        "c1": Baseline(catalog=engine.oracle.catalog, profile=estate.manifest.runtime_profile),
        "c2": load_staged_catalog(run_root, bytes_),
        "carried_state": engine._carried(T0),
        "decision_index": engine.decisions,
        "state_machine_version": estate.manifest.state_machine_version,
        "at": T0,
    }
    return StagedContext(**{**fields, **overrides})


def test_pr28_phase_one_accepts_a_valid_candidate(tmp_path: Path) -> None:
    """The positive case the sweep below is measured against: without it,
    every refusal could be the fixture's rather than the rule's."""
    from dsl41.boundary import validate_staged

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    verdict = validate_staged(_staged_context(run_root, engine))
    assert verdict.refused == ()
    _close(engine)


def test_pr28_phase_one_refuses_each_of_its_own_checks(tmp_path: Path) -> None:
    """ss8's readiness, one injected failure per check, each matched on the
    message only its own rule produces -- so a case caught by a
    neighbouring rule cannot pass unnoticed."""
    from dsl41.boundary import validate_staged

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    base = _staged_context(run_root, engine)

    # an artifact version this binary does not implement (PR-08d)
    with pytest.raises(EngineError, match="this binary implements"):
        validate_staged(
            _staged_context(
                run_root,
                engine,
                staged=base.staged.model_copy(update={"artifact_format_version": 99}),
            )
        )
    # the candidate and the staged bytes must describe one estate
    with pytest.raises(EngineError, match="the staged bytes do not describe"):
        validate_staged(
            _staged_context(
                run_root,
                engine,
                staged=base.staged.model_copy(update={"source_bundle_hash": "sha256:" + "9" * 64}),
            )
        )
    # the catalog hash must be the loaded bundle's
    with pytest.raises(EngineError, match="the boundary would open a catalog"):
        validate_staged(_staged_context(run_root, engine, c2=engine.oracle.catalog))
    # a profile tampered with beside its recorded hash
    tampered = base.staged_bytes.model_copy(
        update={"runtime_profile": RuntimeProfile(default_tz="Europe/Zurich")}
    )
    with pytest.raises(EngineError, match="a tampered profile beside the original hash"):
        validate_staged(_staged_context(run_root, engine, staged_bytes=tampered))
    # preflight (ss8): a candidate whose identity is sound and whose
    # estate this binary refuses to run
    broken = "insert_job: a\njob_type: c\ncommand: x\nmachine: nowhere\n"
    with pytest.raises(EngineError, match="does not pass preflight"):
        validate_staged(_staged_context(run_root, engine, text=broken))
    # the SM-version check (PR-17): the candidate names the version its own
    # binary implements, and this period runs another one. Injected on the
    # PERIOD's side, because the candidate's `stage_digest` is derived from
    # its fields -- a bumped copy is refused by the identity check first,
    # and the message would then be a neighbouring rule's
    with pytest.raises(EngineError, match="one executable implements one version"):
        validate_staged(
            _staged_context(run_root, engine, state_machine_version=STATE_MACHINE_VERSION + 1)
        )
    _close(engine)


def test_pr17_a_profile_change_and_a_no_change_seal_are_both_transitions(
    tmp_path: Path,
) -> None:
    """PR-17: a transition is not the same thing as a catalog change.

    A runtime-profile change with no catalog change is a transition -- the
    profile is half of C, so moving it moves the period even though every
    job is identical. A seal with NOTHING changed is a transition too: the
    boundary is what closes, and an estate is free to have moved nothing.
    Both are the same rule, which is why refusing either as a no-op would
    leave an operator with no way to cut a period at a chosen instant.

    PR-17's third clause -- a `next_period` whose `state_machine_version`
    differs from the seal's is refused at readiness -- is the SM gate, and
    it is pinned by `test_pr28_readiness_refuses_while_c1_is_open_and_untouched`
    and by the phase-1 table in `test_pr28_phase_one_refuses_each_of_its_own_checks`.
    """
    moved = tmp_path / "profile"
    engine = _genesis(moved)
    closing = read_period_manifest(moved, 1)
    assert closing is not None
    # the same JIL, staged again, under a profile the estate did not have
    staged = _stage(moved, C1_JIL, profile=RuntimeProfile(default_tz="Europe/Zurich"))
    boundary = asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)
    assert boundary.seal.period_id == 1
    opened = read_period_manifest(moved, 2)
    assert opened is not None and opened.period_id == 2
    assert opened.catalog_hash == closing.catalog_hash  # the catalog did not move
    assert opened.runtime_hash != closing.runtime_hash  # the profile did

    still = tmp_path / "unchanged"
    engine = _genesis(still)
    closing = read_period_manifest(still, 1)
    assert closing is not None
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(still, C1_JIL))))
    _close(engine)
    opened = read_period_manifest(still, 2)
    assert opened is not None and opened.period_id == 2
    # nothing about C moved, and the period still did
    assert (opened.catalog_hash, opened.runtime_hash) == (
        closing.catalog_hash,
        closing.runtime_hash,
    )
    assert opened.first_index == boundary.seal.closes_at_index + 1
    assert read_seal(still, 1).digest == boundary.seal.digest


def test_pr30c_an_ordinary_command_and_a_seal_cannot_share_a_request_id(
    tmp_path: Path,
) -> None:
    """PR-30c's other half: `request_id` collides across the WHOLE period,
    not only seal-to-seal. One `request_id`, one command
    (`control-protocol.md` ss3) -- so an ordinary `STARTJOB` R and a `seal`
    R cannot both name authoritative decisions."""
    from dsl41.boundary import validate_staged
    from dsl41.runner_admission import Envelope

    run_root = tmp_path / "run"
    engine = _genesis(run_root)

    async def under_c1() -> None:
        decided = engine.submit(
            Event(at=T0, kind="ON_HOLD", payload={"job": "b"}),
            Envelope(request_id="r-seal-1", expect={"job:b": 0}, epoch=engine.epoch),
        )
        await engine.run_until_quiescent(T0)
        await decided

    asyncio.run(under_c1())
    with pytest.raises(EngineError, match="already names another command"):
        validate_staged(_staged_context(run_root, engine))  # the request_id is `r-seal-1`
    _close(engine)


def test_pr28_phase_one_refuses_an_r_verdict_while_c1_is_untouched(tmp_path: Path) -> None:
    """The R gate at readiness: a period never opens over live work whose
    closure changed, and the refusal happens while C1 is still open and
    correct."""
    from dsl41.boundary import validate_staged

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    _start_a(engine)  # `a` is RUNNING under C1
    changed = (
        "insert_job: a\njob_type: c\ncommand: DIFFERENT\n\ninsert_job: b\njob_type: c\ncommand: y\n"
    )
    staged = _stage(run_root, changed)
    message = asyncio.run(_refused(engine, _request(engine, staged)))
    assert "the classification refuses the boundary" in message
    assert engine.oracle.store.runtime("a").status == "RUNNING"  # untouched
    with pytest.raises(EngineError, match="never opens over live work"):
        validate_staged(_staged_context(run_root, engine, text=changed))
    _close(engine)


@pytest.mark.parametrize("state", ["applied", "indeterminate", "retired", "absent"])
def test_pr33_a_live_wrapper_under_a_terminal_row_is_re_driven(tmp_path: Path, state: str) -> None:
    """PR-33, table-driven over the KILL effect's recorded state.

    `_apply_kill` records `applied` when the cancellation is delivered and
    the TERM/grace/KILL ladder runs on the way out of the task, so an
    engine that dies mid-ladder leaves a live wrapper under a terminal row
    with the effect ALREADY RESOLVED. Re-driving only pending kills read
    that state and walked past it, and the row being terminal is exactly
    what makes the process an orphan: the sweep skips it as "already
    replayed" and nothing else ever looks again."""
    from dsl41.runner_adapters import SupervisedCommandAdapter
    from dsl41.runner_effects import EffectOutcome
    from dsl41.runner_startup import _redrive_orphans

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    _start_a(engine)
    effect = next(e for e in engine.outbox.effects() if e.kind == "SPAWN")
    if state != "absent":
        kill = effect.model_copy(update={"effect_id": "e9:KILL:a.1", "kind": "KILL"})
        engine.outbox.record(kill)
        engine.outbox.resolve(
            EffectOutcome(effect_id=kill.effect_id, state=state, run_id=effect.run_id)  # type: ignore[arg-type]
        )
    engine.inject(Event(at=T0, kind="STATUS", payload={"job": "a", "status": "TERMINATED"}))
    asyncio.run(engine.run_until_quiescent(T0))
    assert engine.oracle.store.runtime("a").status == "TERMINATED"

    signalled: list[str] = []

    class _Adapter(SupervisedCommandAdapter):
        def __init__(self) -> None:  # no supervisor client: this rung only signals
            pass

        async def kill(self, run_id: str) -> None:
            signalled.append(run_id)

    engine.adapters["CMD"] = _Adapter()
    listing = {("a", 1): {"wrapper_alive": True, "run_id": effect.run_id}}
    asyncio.run(_redrive_orphans(engine, listing, set()))
    assert signalled == [effect.run_id]  # whatever the KILL effect said
    # and a run this resume already killed is not signalled twice
    signalled.clear()
    asyncio.run(_redrive_orphans(engine, listing, {str(effect.run_id)}))
    assert signalled == []
    _close(engine)


# --------------------------------------- sol round-1 pins (U6B-01..10, DL-133)


def test_pr01c_a_sentinelless_root_with_estate_dirs_refuses_genesis(tmp_path: Path) -> None:
    """ss1.1's unused-root predicate is COMPLETE: no sentinel proves
    nothing when `wal/` or a populated `runs/` is already there -- a
    genesis would relabel foreign work under a fresh estate_id."""
    catalog, sources = _catalog(C1_JIL)
    for leftover in ("wal", "seals", "runs", "periods"):
        run_root = tmp_path / f"root-{leftover}"
        (run_root / leftover).mkdir(parents=True)
        if leftover == "runs":
            (run_root / "runs" / "a.1").mkdir()
        if leftover == "periods":
            (run_root / "periods" / "000001").mkdir()  # COMMITTED -- .staging alone is fine
        staged = stage_manifest(
            catalog,
            source_bundle_hash=write_bundle(run_root, sources),
            profile=RuntimeProfile(),
            state_machine_version=STATE_MACHINE_VERSION,
        )
        with pytest.raises(EngineError, match="not an unused root"):
            start_run(
                catalog,
                run_root,
                clock=VirtualClock(start=T0),
                adapters={"CMD": FakeAdapter(default=None)},
                staged=staged,
            )
        assert read_sentinel(run_root) is None  # the refusal wrote nothing


def _bind_a(engine, run_root: Path, *, run_id: str | None = None) -> Any:
    """Start `a` and give its applied SPAWN the ss3.5 spool binding."""
    _start_a(engine)
    effect = next(e for e in engine.outbox.effects() if e.kind == "SPAWN")
    run_dir = run_root / "runs" / "a.1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "spawn.json").write_text(
        json.dumps(
            {
                "run_id": run_id or effect.run_id,
                "job": "a",
                "run_number": 1,
                "boot_id": "boot-of-a-previous-life",
            }
        )
    )
    return effect


def test_an_applied_binding_is_restored_into_the_outbox_at_resume(tmp_path: Path) -> None:
    """ss3.5: a sealed `bound` execution is an APPLIED outbox binding in
    C2, not just a carried row -- without it, reconciliation has no bound
    SPAWN and would accept whatever identity a LIST or spool claims."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root, profile=DETACHED)
    effect = _bind_a(engine, run_root)
    engine.supervisor = _StubSupervisor(  # ss8: a detached seal proves its supervisor
        listing={
            "incarnation": "inc-1",
            "runs": [{"run_id": effect.run_id, "job": "a", "run_number": 1, "wrapper_alive": True}],
        }
    )  # type: ignore[assignment]
    boundary = asyncio.run(
        _seal(engine, _request(engine, _stage(run_root, C2_JIL, profile=DETACHED)))
    )
    _close(engine)
    assert boundary.seal.executions[0].kind == "bound"

    opened = _resume(run_root, C2_JIL)
    _close(opened)
    restored = [e for e in opened.outbox.effects() if e.kind == "SPAWN"]
    assert [e.run_id for e in restored] == [effect.run_id]
    assert opened.outbox.state_of(effect.effect_id) == "applied"
    assert not [e for e in opened.outbox.pending() if e.kind == "SPAWN"]  # applied, not pending


def test_recovery_fsyncs_the_closing_wal_before_the_cas(tmp_path: Path, monkeypatch) -> None:
    """ss11: "recovery read the seal line" proves readable, not durable.
    The CAS is performed only after the closing WAL is fsynced, and an
    fsync failure stays fail-stopped with the head unmoved."""
    import dsl41.boundary as boundary_mod

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    engine.crash_point = _crash_at("after_seal_record")  # type: ignore[method-assign]
    request = _request(engine, _stage(run_root, C2_JIL))

    async def scenario() -> None:
        engine.submit_seal(request)
        with pytest.raises(EngineError, match="did not complete cleanly"):
            await engine.run_until_quiescent(T0)

    asyncio.run(scenario())
    _close(engine)

    def broken(path: Path) -> None:
        raise OSError(f"fsync of {path} failed")

    monkeypatch.setattr(boundary_mod, "fsync_file", broken)
    with pytest.raises(OSError, match="fsync of"):
        _resume(run_root, C2_JIL)
    stored = EstateAnchor(default_anchor_dir(run_root)).read()
    assert stored is not None and isinstance(stored.head, OpenHead)
    assert stored.head.period_id == 1  # the CAS did NOT run over an undurable line
    monkeypatch.undo()
    opened = _resume(run_root, C2_JIL)
    _close(opened)
    assert opened.oracle.store.period_id == 2


def test_the_seal_append_is_fsynced_in_the_virtual_domain_too(tmp_path: Path, monkeypatch) -> None:
    """ss2.2: a virtual-domain journal buffers ordinary appends, and the
    anchor CAS follows the seal line at once -- so `Journal.seal` fsyncs
    unconditionally, or a power cut can keep the successor and lose the
    line that names it."""
    import os as os_mod

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    assert engine.journal is not None
    journal_fd = engine.journal._f.fileno()
    real_fsync = os_mod.fsync
    synced: list[int] = []

    def recording(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("os.fsync", recording)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    monkeypatch.undo()
    _close(engine)
    assert journal_fd in synced  # the CLOSING journal's fd, not just the sidecar's


def test_the_run_root_is_fsynced_when_the_first_boundary_creates_seals(
    tmp_path: Path, monkeypatch
) -> None:
    """The run root's directory entry for `seals/` is a record too: without
    its fsync, a power cut can keep a durable seal line and lose the
    directory the sidecar lives in."""
    import dsl41.boundary as boundary_mod

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    request = _request(engine, _stage(run_root, C2_JIL))
    real = boundary_mod.fsync_dir
    fsynced: list[Path] = []

    def recording(path: Path) -> None:
        fsynced.append(path)
        real(path)

    monkeypatch.setattr(boundary_mod, "fsync_dir", recording)
    asyncio.run(_seal(engine, request))
    monkeypatch.undo()
    _close(engine)
    assert run_root in fsynced


def _ident(path: Path) -> tuple[int, int]:
    """What a directory entry IS, independent of its name: a rename moves the
    inode, so the staged directory and `periods/N+1/` are the same thing."""
    stat = path.stat()
    return (stat.st_dev, stat.st_ino)


def _boundary_fsyncs(
    run_root: Path, *, reuse: bool, drop: str | None = None
) -> tuple[set[tuple[int, int]], dict[str, tuple[int, int]]]:
    """Drive one whole boundary; report what it fsynced and the identity of
    each artifact PR-30g requires durable.

    `drop` names one of those fsyncs and REMOVES it -- what deleting that
    line from the liturgy would do. The witness records identities rather
    than call sites, so the assertion is about the entry that was made
    durable and not about which function did it: a second call site fsyncing
    the same directory keeps the test green, and rightly.

    `reuse=True` takes the SAME-STAGE path: the first attempt installs
    `periods/000002/` and dies before the seal, and the retry rewrites the
    manifest in place (ss7, PR-30f). The manifest's own two fsyncs are
    dropped by position inside its write, which is the one place both paths
    spell them the same way."""
    import dsl41.boundary as boundary_mod
    import dsl41.period as period_mod

    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)
    if reuse:
        engine.crash_point = _crash_at("after_install")  # type: ignore[method-assign]
        asyncio.run(_refused(engine, _request(engine, staged)))
        engine.crash_point = _no_crash  # type: ignore[method-assign]
    seen: set[tuple[int, int]] = set()
    writing_manifest = [False, 0]
    real_fsync, real_dir, real_write = os.fsync, boundary_mod.fsync_dir, boundary_mod.durable_write
    dropped_dir = {
        "staging": run_root / "periods" / ".staging",
        "periods": run_root / "periods",
    }.get(drop or "")

    def fsync(fd: int) -> None:
        if writing_manifest[0]:
            position = writing_manifest[1]
            writing_manifest[1] = position + 1
            if drop == ("manifest" if position == 0 else "period_dir"):
                return
        stat = os.fstat(fd)
        seen.add((stat.st_dev, stat.st_ino))
        real_fsync(fd)

    def fsync_dir(path: Path | str) -> None:
        if dropped_dir is not None and Path(path) == dropped_dir:
            return
        real_dir(path)

    def durable_write(path: str, data: bytes) -> None:
        if os.path.basename(path) != "manifest.json":
            real_write(path, data)
            return
        writing_manifest[0], writing_manifest[1] = True, 0
        try:
            real_write(path, data)
        finally:
            writing_manifest[0] = False

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("os.fsync", fsync)
        for module in (boundary_mod, period_mod):
            patch.setattr(module, "fsync_dir", fsync_dir)
            patch.setattr(module, "durable_write", durable_write)
        asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)
    installed = period_dir(run_root, 2)
    required = {
        "manifest": _ident(installed / "manifest.json"),
        "period_dir": _ident(installed),
        "periods": _ident(installed.parent),
    }
    if not reuse:  # the reuse path renames nothing out of `.staging`
        required["staging"] = _ident(run_root / "periods" / ".staging")
    return seen, required


@pytest.mark.parametrize("reuse", [False, True], ids=["fresh-install", "same-stage-reuse"])
def test_pr30g_the_committed_period_survives_a_power_loss_after_the_seal(
    tmp_path: Path, reuse: bool
) -> None:
    """PR-30g: after the committed seal, `periods/N+1/` and its
    `manifest.json` are on the disk -- a seal that names a manifest the
    machine can still lose is a lineage that cannot open.

    Both install paths owe it: the fresh one renames a staged directory into
    place and fsyncs four entries, the same-stage reuse path rewrites the
    manifest by its own in-place liturgy. Each fsync is removed in turn and
    the check must fail on exactly the missing one -- a test that counted
    fsyncs would stay green with the wrong one gone."""
    whole, required = _boundary_fsyncs(tmp_path / "whole", reuse=reuse)
    assert set(required.values()) <= whole
    for name in required:
        mutated, expected = _boundary_fsyncs(tmp_path / name, reuse=reuse, drop=name)
        assert expected[name] not in mutated, name  # the one that was removed
        assert {i for k, i in expected.items() if k != name} <= mutated  # and only it


def test_a_pre_ponr_oserror_aborts_the_boundary_and_the_retry_commits(tmp_path: Path) -> None:
    """ss7: EVERY pre-PONR exception aborts -- an `OSError` from a write,
    fsync or rename is exactly as reversible as an `EngineError`, and
    leaving admission frozen behind one wedges the engine."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)

    def hook(name: str) -> None:
        if name == "after_sidecar":
            raise OSError("disk gone under the sidecar")

    engine.crash_point = hook  # type: ignore[method-assign]

    async def scenario() -> None:
        future = engine.submit_seal(_request(engine, staged))
        await engine.run_until_quiescent(T0)
        assert future.done()
        with pytest.raises(OSError, match="disk gone"):
            future.result()

    asyncio.run(scenario())
    assert engine.sealing is False and engine.barrier.parked is False
    engine.inject(Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": "on"}))
    asyncio.run(engine.run_until_quiescent(T0))
    assert engine.oracle.store.global_value("G") == "on"  # C1 is open and correct
    engine.crash_point = _no_crash  # type: ignore[method-assign]
    boundary = asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)
    assert boundary.seal.next_period.stage_digest == staged.stage_digest


def test_durable_write_cleans_its_temp_and_survives_a_stale_one(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed durability liturgy leaves no half-written temp behind, and
    a stale temp from an earlier failed attempt never wedges the retry
    (the O_EXCL create would EEXIST forever)."""
    import os as os_mod

    from dsl41.runner_procid import durable_write

    target = tmp_path / "doc.json"
    tmp = tmp_path / f".doc.json.{os_mod.getpid()}.tmp"
    tmp.write_bytes(b"half-written by a previous attempt")
    durable_write(str(target), b"good")  # the stale temp does not wedge this
    assert target.read_bytes() == b"good" and not tmp.exists()

    real_rename = os_mod.rename

    def broken(src: str, dst: str) -> None:
        raise OSError("rename lost the disk")

    monkeypatch.setattr("os.rename", broken)
    with pytest.raises(OSError, match="rename lost"):
        durable_write(str(target), b"newer")
    monkeypatch.undo()
    assert not tmp.exists()  # the failure cleaned up after itself
    durable_write(str(target), b"newer")  # and the retry is not wedged
    assert target.read_bytes() == b"newer"
    monkeypatch.setattr("os.rename", real_rename)


def test_the_seal_refuses_a_strangers_watch_log(tmp_path: Path) -> None:
    """DL-118 at the seal: a valid `watch.jsonl` that names another run is
    a stranger's, and reading its progress would relabel that stranger's
    state as this run's and commit it."""
    from dsl41.canon import ARTIFACT_FORMAT_VERSION

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    engine.QUIESCE_WAIT_S = 0.05
    _start_a(engine)
    run_dir = run_root / "runs" / "a.1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "watch.jsonl").write_text(
        json.dumps(
            {
                "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                "at": T0.isoformat(),
                "kind": "start",
                "run_id": str(uuid.uuid4()),  # valid, and NOT this run's
            },
            sort_keys=True,
        )
        + "\n"
    )
    message = asyncio.run(_refused(engine, _request(engine, _stage(run_root, C2_JIL))))
    assert "refusing to seal a stranger's watch" in message
    _close(engine)


def test_the_seal_refuses_a_strangers_spawn_binding(tmp_path: Path) -> None:
    """Same rule, `spawn.json`: existence is not identity."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    engine.QUIESCE_WAIT_S = 0.05
    _bind_a(engine, run_root, run_id=str(uuid.uuid4()))
    message = asyncio.run(_refused(engine, _request(engine, _stage(run_root, C2_JIL))))
    assert "refusing to seal a stranger's binding" in message
    _close(engine)


class _StubSupervisor:
    """Just enough of SupervisorClient for the ss8 proof: an incarnation
    and a LIST answer (or the refusal to give one)."""

    def __init__(
        self,
        *,
        incarnation: str | None = "inc-1",
        listing: dict[str, Any] | None = None,
        unreachable: bool = False,
    ) -> None:
        self.incarnation = incarnation
        self.listing = listing or {"incarnation": incarnation, "runs": []}
        self.unreachable = unreachable

    async def list_runs(self) -> dict[str, Any]:
        if self.unreachable:
            raise SupervisorUnavailable("socket gone")
        return self.listing


def test_pr27_the_seal_proves_the_supervisor_before_it_commits(tmp_path: Path) -> None:
    """ss8's supervisor clauses at the seal, in one lineage: unreachable
    with live detached work refuses; a restarted incarnation's LIST is not
    proof; a carried bound run missing from LIST refuses; an identity
    split refuses; a live run the seal does not carry refuses; and the
    reconciled LIST commits."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root, profile=DETACHED)
    effect = _bind_a(engine, run_root)
    staged = _stage(run_root, C2_JIL, profile=DETACHED)

    def row(**overrides: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "run_id": effect.run_id,
            "job": "a",
            "run_number": 1,
            "wrapper_alive": True,
        }
        return {**fields, **overrides}

    cases: list[tuple[_StubSupervisor, str]] = [
        (_StubSupervisor(unreachable=True), "quiescence is unprovable"),
        (
            _StubSupervisor(listing={"incarnation": "inc-2", "runs": [row()]}),
            "restarted supervisor's history is not proof",
        ),
        (_StubSupervisor(), "not in the leased incarnation's LIST"),
        (
            _StubSupervisor(
                listing={"incarnation": "inc-1", "runs": [row(run_id=str(uuid.uuid4()))]}
            ),
            "identity split at the seal",
        ),
        (
            _StubSupervisor(
                listing={
                    "incarnation": "inc-1",
                    "runs": [row(), row(job="b", run_number=7, run_id=str(uuid.uuid4()))],
                }
            ),
            "evidence quiescence cannot account for",
        ),
    ]
    for stub, fragment in cases:
        engine.supervisor = stub  # type: ignore[assignment]
        assert fragment in asyncio.run(_refused(engine, _request(engine, staged)))
    engine.supervisor = _StubSupervisor(  # type: ignore[assignment]
        listing={"incarnation": "inc-1", "runs": [row()]}
    )
    boundary = asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)
    assert boundary.seal.executions[0].run_id == effect.run_id


def test_pr27_a_dead_wrapper_in_the_list_is_history_not_unaccounted_evidence(
    tmp_path: Path,
) -> None:
    """The LIST may carry a row for a run whose wrapper already exited --
    its outcome resolves from the spool, not from this proof -- so that row
    must not read as live evidence quiescence cannot account for, even
    though it names a run this seal does not carry at all. The lineage
    above already proves the SAME shape refuses when `wrapper_alive` is
    True (job b, run 7); this is the other side of that branch."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root, profile=DETACHED)
    effect = _bind_a(engine, run_root)
    staged = _stage(run_root, C2_JIL, profile=DETACHED)
    engine.supervisor = _StubSupervisor(  # type: ignore[assignment]
        listing={
            "incarnation": "inc-1",
            "runs": [
                {"run_id": effect.run_id, "job": "a", "run_number": 1, "wrapper_alive": True},
                {
                    "run_id": str(uuid.uuid4()),
                    "job": "b",
                    "run_number": 7,
                    "wrapper_alive": False,
                },
            ],
        }
    )
    boundary = asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)
    assert boundary.seal.executions[0].run_id == effect.run_id


def test_pr27a_a_dead_supervisor_that_owns_nothing_does_not_block_the_seal(
    tmp_path: Path,
) -> None:
    """PR-27a: no live detached work, no unresolved evidence -- a
    permanently dead machine must not block every future seal."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    engine.supervisor = _StubSupervisor(unreachable=True)  # type: ignore[assignment]
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    assert boundary.seal.executions == ()


def test_every_duplicated_seal_record_field_is_compared_at_recovery(tmp_path: Path) -> None:
    """ss11: `source`, `request_id`, `claimed_actor` and `force_seal` are
    duplicated into the record, so recovery compares them too -- a
    disagreement on ANY duplicated field refuses the sidecar."""
    from dsl41.boundary import _record_disagreements

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    assert _record_disagreements(boundary.seal, boundary.record) == []
    for key, wrong in (
        ("source", "someone-elses-source"),
        ("request_id", "someone-elses-request"),
        ("claimed_actor", "mallory@ops"),
        ("force_seal", True),
    ):
        mutated = {**boundary.record, key: wrong}
        disagreements = _record_disagreements(boundary.seal, mutated)
        assert any(key in d for d in disagreements), key


def test_a_resume_through_that_window_refuses_before_the_head_moves(tmp_path: Path) -> None:
    """DL-145. The operator's route to that crash window is `dsl41 run
    --resume`, and it reached a WEAKER rule than the opener's.

    `open_next_period` demands AGREEMENT before it moves the head --
    `_check_existing_segment` first, then the CAS, which is DL-142's pinned
    order. `act_on_head`'s `claimed` row performed the same CAS on the
    segment's mere PRESENCE, so a disagreeing segment moved the head and
    was caught afterwards, by a later gate, with the lineage already
    naming it.

    One evidence standard now. Same window, same disagreement as the test
    above; the difference is only which door the estate is opened
    through."""
    run_root = tmp_path / "run"
    stranger = "sha256:" + "b" * 64
    with _crashed_between_the_segment_and_the_cas(run_root):
        path = wal_path(run_root, 2)
        lines = path.read_text().splitlines()
        segment = {**json.loads(lines[0]), "runtime_hash": stranger}
        path.write_text("\n".join([json.dumps(segment, sort_keys=True), *lines[1:]]) + "\n")
    # the fixture's lock is released here: recovery takes the lineage lock
    # itself, which is what makes this the operator's route and not the
    # opener's
    with pytest.raises(EngineError, match="did not open") as refused:
        _resume(run_root, C2_JIL)
    assert f"runtime_hash: segment {stranger!r}" in str(refused.value)
    stored = EstateAnchor(default_anchor_dir(run_root)).read()
    assert stored is not None
    assert isinstance(stored.head, ClaimedHead)  # the CAS never ran


def test_a_claimed_head_whose_root_holds_no_naming_seal_refuses(tmp_path: Path) -> None:
    """The other arm of the same check (DL-145): the head move is licensed
    by the seal the segment opened from, and the agreement check needs it.

    A `claimed` head pointing at a period whose segment this root holds,
    with no seal in the lineage that names it, is a state nothing this
    binary writes -- so it refuses by name rather than performing a CAS it
    cannot justify."""
    run_root = tmp_path / "run"
    with _crashed_between_the_segment_and_the_cas(run_root) as (anchor, committed, _catalog_c2):
        from dsl41.boundary import Lineage, act_on_head

        with pytest.raises(EngineError, match="no seal that opens the claimed period"):
            act_on_head(
                anchor,
                run_root=run_root,
                estate_id=committed.seal.estate_id,
                lineage=Lineage(seal=None, opens_next=False),
            )
        stored = anchor.require(committed.seal.estate_id)
    assert isinstance(stored.head, ClaimedHead)


# ------------------------------------------- sol round-2 pins (U6BR2, DL-133)


def test_a_refused_genesis_releases_both_locks_for_a_same_process_retry(tmp_path: Path) -> None:
    """ss1.1: a genesis that fails AFTER the claim -- a staged profile the
    wiring disagrees with, a gate, a WAL error -- holds nothing. Both
    raw-fd locks conflict with a retry in this same process, so leaving
    them held would wedge every embedder and test that retries."""
    run_root = tmp_path / "run"
    catalog, sources = _catalog(C1_JIL)
    wrong = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(default_tz="Europe/Zurich"),  # the wiring says UTC
        state_machine_version=STATE_MACHINE_VERSION,
    )
    from dsl41.runner_scheduler import Scheduler

    scheduler = Scheduler(catalog, start=T0, default_tz="UTC")
    with pytest.raises(EngineError, match="disagrees with the engine's wiring"):
        start_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=scheduler,
            staged=wrong,
        )
    engine = _genesis(run_root)  # the SAME process retries and is not wedged
    _close(engine)
    assert read_sentinel(run_root) is not None


def test_a_tethered_estate_with_a_live_command_refuses_the_seal(tmp_path: Path) -> None:
    """The ss8 mode table: "in place, tethered -- full drain". Stopping the
    engine cancels a tethered command, so a seal that carried it as
    `bound` would name a run the exit code 3 then kills."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)  # default profile: tethered, the CLI default
    engine.QUIESCE_WAIT_S = 0.05
    _bind_a(engine, run_root)
    message = asyncio.run(_refused(engine, _request(engine, _stage(run_root, C2_JIL))))
    assert "tethered estate with live command(s) a.1" in message
    assert "full drain" in message
    _close(engine)


def test_a_detached_estate_without_a_client_cannot_prove_the_seal(tmp_path: Path) -> None:
    """ss8: a detached estate's live work is owned by a supervisor, and an
    engine holding no client cannot prove that supervisor reachable --
    quiescence is unprovable, not vacuous."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root, profile=DETACHED)
    _bind_a(engine, run_root)
    assert engine.supervisor is None
    message = asyncio.run(
        _refused(engine, _request(engine, _stage(run_root, C2_JIL, profile=DETACHED)))
    )
    assert "holds no supervisor client" in message
    _close(engine)


def test_resume_binds_the_supervisor_client_to_the_engine(tmp_path: Path) -> None:
    """PR-27 in core, not in the CLI: an embedder that resumes with a
    client must get an engine whose seal can prove the ss8 clauses."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    _close(engine)
    stub = _StubSupervisor()
    catalog, _ = _catalog(C1_JIL)
    opened = asyncio.run(
        resume_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            supervisor=stub,  # type: ignore[arg-type]
        )
    )
    _close(opened)
    assert opened.supervisor is stub


def test_an_input_arriving_during_the_supervisor_proof_is_drained_before_the_snapshot(
    tmp_path: Path,
) -> None:
    """ss8: the proof AWAITS the supervisor, and a completion landing in
    that window must be drained and re-proved -- not snapshotted half-in,
    not dropped."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)

    class _Injecting(_StubSupervisor):
        def __init__(self) -> None:
            super().__init__()
            self.injected = False

        async def list_runs(self) -> dict[str, Any]:
            if not self.injected:
                self.injected = True
                engine.inject(
                    Event(at=T0, kind="SET_GLOBAL", payload={"name": "LATE", "value": "in"})
                )
            return await super().list_runs()

    stub = _Injecting()
    engine.supervisor = stub  # type: ignore[assignment]
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    assert stub.injected
    assert boundary.seal.state.globals["LATE"].value == "in"  # drained, then sealed


def test_inputs_that_never_stop_arriving_during_the_proof_time_out(tmp_path: Path) -> None:
    """The counterpart to the single latecomer above: if a NEW input lands on
    every pass -- drained, then another arrives during the very next proof --
    the estate never reaches quiescence with an empty queue at once, and the
    ss8 loop must not spin forever. It refuses once QUIESCE_WAIT_S has passed
    (period-model ss8)."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    engine.QUIESCE_WAIT_S = 0.05  # the wait is real; the refusal is what ends it

    class _NeverSettles(_StubSupervisor):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def list_runs(self) -> dict[str, Any]:
            self.calls += 1  # every call re-arms the queue: nothing ever settles
            engine.inject(
                Event(at=T0, kind="SET_GLOBAL", payload={"name": "LATE", "value": str(self.calls)})
            )
            return await super().list_runs()

    stub = _NeverSettles()
    engine.supervisor = stub  # type: ignore[assignment]
    message = asyncio.run(_refused(engine, _request(engine, _stage(run_root, C2_JIL))))
    assert "inputs keep arriving during the ss8 supervisor proof" in message
    assert stub.calls > 1  # more than one pass really happened before the timeout
    _close(engine)


def test_a_seal_whose_fsync_fails_reaches_no_subscriber(tmp_path: Path, monkeypatch) -> None:
    """ss2.2: durability comes BEFORE subscriber publication. A subscriber
    handed a commit record whose fsync then fails has been told about a
    boundary recovery may discard."""
    import os as os_mod

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    assert engine.journal is not None
    queue = engine.journal.subscribe()
    journal_fd = engine.journal._f.fileno()
    real_fsync = os_mod.fsync

    def broken(fd: int) -> None:
        if fd == journal_fd:
            raise OSError("fsync lost the disk")
        real_fsync(fd)

    monkeypatch.setattr("os.fsync", broken)
    message = ""
    try:
        asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    except EngineError as exc:
        message = str(exc)
    monkeypatch.undo()
    assert "UNKNOWN" in message  # the fail-stop, not a commit
    drained = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    assert not any(r.get("rec") == "seal" for r in drained)
    engine.journal.unsubscribe(queue)
    _close(engine)


def test_a_seal_records_catalog_hash_version_is_an_exact_integer(tmp_path: Path) -> None:
    """ss2.2 is verbatim: `2.0` and `True`-shaped versions are not `2`."""
    from dsl41.boundary import check_seal_record, seal_record

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    record = seal_record(boundary.seal)
    check_seal_record(record)  # the genuine record passes
    for wrong in (2.0, True):
        with pytest.raises(EngineError, match="catalog_hash_version"):
            check_seal_record({**record, "catalog_hash_version": wrong})


def test_the_two_ss2_record_liturgies_are_one_engine(tmp_path: Path) -> None:
    """DL-137: `check_segment_record` (ss2.1) and `check_seal_record`
    (ss2.2) spelled the same exact-schema walk in two bodies -- unknown
    key first, then per field missing before mistyped, with the record
    kind and the citation in every message.

    One engine now (`period.check_record_fields`), and this is what makes
    that structural rather than a claim: the SAME three faults are injected
    into both records and both refusals are compared against one template.
    A weakened, reworded or reordered engine reds both halves of this test
    at once, which is exactly what two hand-written copies could not do."""
    from dsl41.boundary import check_seal_record, seal_record
    from dsl41.period import check_segment_record

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    liturgies = (
        (check_segment_record, read_journal(wal_path(run_root, 1))[0], "segment", "ss2.1"),
        (check_seal_record, seal_record(boundary.seal), "seal", "ss2.2"),
    )
    for check, record, kind, section in liturgies:
        check(record)  # the writer's own shape passes
        cite = f"period-model {section}"
        faults = (
            ({**record, "surprise": 1}, f"{kind} record carries unknown surprise"),
            (
                {k: v for k, v in record.items() if k != "estate_id"},
                f"{kind} record missing estate_id ({cite})",
            ),
            ({**record, "estate_id": 7}, f"{kind} record: estate_id is 7 ({cite})"),
        )
        for faulty, expected in faults:
            with pytest.raises(EngineError) as refused:
                check(faulty)
            assert str(refused.value) == expected


def test_genesis_fsyncs_the_parents_of_the_root_and_the_anchor(tmp_path: Path, monkeypatch) -> None:
    """The parent's entry for a created run root or anchor directory is a
    record too: without its fsync a power cut can retain one lineage half
    and lose the other's directory entry."""
    import os as os_mod

    from dsl41.runner_ledger import acquire_run_root

    real_fsync = os_mod.fsync
    synced: set[int] = set()

    def recording(fd: int) -> None:
        synced.add(os_mod.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr("os.fsync", recording)
    root = tmp_path / "estates" / "run"
    lock = acquire_run_root(root)
    assert os_mod.stat(root.parent).st_ino in synced
    # mkdir(parents=True) created "estates" too: ITS parent's entry is a
    # record as well, not just the immediate one
    assert os_mod.stat(tmp_path).st_ino in synced
    lock.release()
    synced.clear()
    lock = acquire_run_root(root)  # retry on an EXISTING root still fsyncs --
    lock.release()  # a crash between an earlier mkdir and its fsync is repaired
    assert os_mod.stat(root.parent).st_ino in synced
    anchor = EstateAnchor(tmp_path / "anchors" / "run.anchor")
    anchor.acquire()
    assert os_mod.stat((tmp_path / "anchors").resolve()).st_ino in synced
    assert os_mod.stat(tmp_path).st_ino in synced
    anchor.release()
    monkeypatch.undo()


# ------------------------------------------- sol round-3 pins (U6BR3, DL-133)


def test_the_opening_segment_record_is_fsynced_before_any_head_action(
    tmp_path: Path, monkeypatch
) -> None:
    """ss1.1/ss11: the opening record NAMES the segment, and the head
    actions (genesis finalize, a claim's open, the boundary's open) rely
    on the file -- so the record is durable at `Journal.create`, virtual
    domain included, not only when recovery happens to re-fsync it."""
    import os as os_mod

    real_fsync = os_mod.fsync
    synced: set[int] = set()

    def recording(fd: int) -> None:
        synced.add(os_mod.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr("os.fsync", recording)
    run_root = tmp_path / "run"
    engine = _genesis(run_root)  # virtual clock: nothing else fsyncs this file
    assert os_mod.stat(wal_path(run_root, 1)).st_ino in synced
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    synced.clear()
    opened = _resume(run_root, C2_JIL)  # the boundary opens the successor segment
    _close(opened)
    assert os_mod.stat(wal_path(run_root, 2)).st_ino in synced
    assert boundary.seal.period_id == 1
    monkeypatch.undo()


def test_an_input_stamped_after_t_refuses_the_boundary(tmp_path: Path) -> None:
    """ss6: C1 owns every instant <= T and nothing after it. An input that
    lands mid-seal stamped past the cutoff cannot be admitted into the
    period being sealed, and re-choosing T would move the boundary under
    the request that composed it -- so the boundary refuses and C1
    reopens."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)

    class _LateInjecting(_StubSupervisor):
        async def list_runs(self) -> dict[str, Any]:
            engine.inject(
                Event(
                    at=T0 + timedelta(hours=1),  # strictly after the cutoff
                    kind="SET_GLOBAL",
                    payload={"name": "LATE", "value": "no"},
                )
            )
            return await super().list_runs()

    engine.supervisor = _LateInjecting()  # type: ignore[assignment]
    message = asyncio.run(_refused(engine, _request(engine, _stage(run_root, C2_JIL))))
    assert "stamped after the cutoff" in message
    # C1 is open and correct: the late input applies as ordinary C1 work now
    asyncio.run(engine.run_until_quiescent(T0 + timedelta(hours=1)))
    assert engine.oracle.store.global_value("LATE") == "no"
    _close(engine)


def test_a_task_that_dies_during_the_proof_fails_the_boundary_loudly(tmp_path: Path) -> None:
    """ss8: the proof awaits the supervisor, and a task that dies in that
    window must surface before the snapshot -- the loop re-settles after
    every proof rather than committing over a corpse."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)

    class _TaskKilling(_StubSupervisor):
        async def list_runs(self) -> dict[str, Any]:
            from dsl41.runner import _LiveRun

            async def boom() -> None:
                raise RuntimeError("adapter task died mid-proof")

            engine._live["ghost"] = _LiveRun(
                run_number=1, task=asyncio.get_running_loop().create_task(boom())
            )
            return await super().list_runs()

    engine.supervisor = _TaskKilling()  # type: ignore[assignment]

    async def scenario() -> None:
        future = engine.submit_seal(_request(engine, _stage(run_root, C2_JIL)))
        # the corpse surfaces BEFORE the snapshot: the boundary aborts (the
        # future carries the failure) and the engine loop itself dies loudly
        # on the same corpse -- an adapter bug is never sealed over
        with pytest.raises(RuntimeError, match="died mid-proof"):
            await engine.run_until_quiescent(T0)
        assert future.done()
        assert isinstance(future.exception(), RuntimeError)

    asyncio.run(scenario())
    assert "ghost" in engine._live  # the corpse is PRESERVED, not consumed by the abort
    stored = EstateAnchor(default_anchor_dir(run_root)).read()
    assert stored is not None and isinstance(stored.head, OpenHead)
    assert stored.head.period_id == 1  # nothing committed over the corpse
    _close(engine)


def test_start_run_creates_the_root_through_the_durable_helper(tmp_path: Path, monkeypatch) -> None:
    """The REAL genesis path: `start_run` must not pre-create the root with
    a plain mkdir, or the durability helper sees an existing directory and
    proves nothing about its dirent."""
    import os as os_mod

    real_fsync = os_mod.fsync
    synced: set[int] = set()

    def recording(fd: int) -> None:
        synced.add(os_mod.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr("os.fsync", recording)
    run_root = tmp_path / "estates" / "run"
    engine = _genesis(run_root)
    _close(engine)
    monkeypatch.undo()
    assert os_mod.stat(run_root.parent).st_ino in synced
    assert os_mod.stat(tmp_path).st_ino in synced


def test_the_watch_fold_takes_a_positional_prefix_and_never_reads_past_it(
    tmp_path: Path,
) -> None:
    """ss2.2/ss3.5: the cut is a COUNT, not an instant (a successor
    unparked at the boundary can poll at exactly T), and lines past the
    prefix are not read at all: an unsupported version a future binary
    wrote must not make a closed period unauditable. A prefix the log
    cannot supply refuses -- the claimed evidence does not exist."""
    from dsl41.canon import ARTIFACT_FORMAT_VERSION
    from dsl41.runner_adapters import read_watch_log

    run_dir = tmp_path / "runs" / "w.1"
    run_dir.mkdir(parents=True)
    rid = str(uuid.uuid4())
    lines: list[dict[str, Any]] = [
        {
            "artifact_format_version": ARTIFACT_FORMAT_VERSION,
            "at": T0.isoformat(),
            "kind": "start",
            "run_id": rid,
        },
        {
            "artifact_format_version": ARTIFACT_FORMAT_VERSION,
            "at": (T0 + timedelta(minutes=1)).isoformat(),
            "kind": "poll",
            "run_id": rid,
            "exists": True,
            "size": 3,
            "qualifying": True,
            "stable_polls": 1,
        },
        {
            # the SUCCESSOR's poll: written by a future binary whose
            # version this one does not implement
            "artifact_format_version": ARTIFACT_FORMAT_VERSION + 1000,
            "at": (T0 + timedelta(minutes=9)).isoformat(),
            "kind": "poll",
            "run_id": rid,
            "exists": True,
            "size": 3,
            "qualifying": True,
            "stable_polls": 2,
        },
    ]
    (run_dir / "watch.jsonl").write_text(
        "".join(json.dumps(rec, sort_keys=True) + "\n" for rec in lines)
    )
    cut = read_watch_log(run_dir, prefix=2)
    assert cut is not None and cut.watch_seq == 2  # the closed period's evidence exactly
    assert cut.stable_polls == 1
    with pytest.raises(EngineError, match="artifact_format_version"):
        read_watch_log(run_dir)  # the LIVE fold still refuses the whole log
    short_dir = tmp_path / "runs" / "s.1"
    short_dir.mkdir(parents=True)
    (short_dir / "watch.jsonl").write_text(
        "".join(json.dumps(rec, sort_keys=True) + "\n" for rec in lines[:2])
    )
    with pytest.raises(EngineError, match="claimed evidence does not exist"):
        read_watch_log(short_dir, prefix=3)  # an over-claim over a valid log


# ------------------------------- ss11 the subscriber across a boundary (DL-135)


class _Recorder:
    """A `StreamWriter` that keeps what was written to it.

    The stream is what these tests are about, so the transport is the part
    that may be a stand-in: a subscription is driven directly and its lines
    are read back, rather than a socket being opened and drained."""

    def __init__(self) -> None:
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        return None

    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in b"".join(self.lines).splitlines() if line]


def _subscribed(engine, run_root: Path, **request: Any) -> list[dict[str, Any]]:
    """One `subscribe` exchange, backfill only: the live half needs a
    record to arrive and these tests are about what came BEFORE."""
    from dsl41.runner_control import ControlServer

    server = ControlServer(engine, run_root / "control.sock")
    writer = _Recorder()

    async def drive() -> None:
        task = asyncio.ensure_future(
            server._subscribe(writer, {"cmd": "subscribe", "v": 3, **request})  # type: ignore[arg-type]
        )
        for _ in range(200):  # the backfill is synchronous; the live half parks
            await asyncio.sleep(0)
            if task.done():
                break
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    return writer.records()


def _two_periods(run_root: Path):
    """A root with period 1 closed and period 2 open, one admitted input
    under each -- so a cursor taken under C1 names an index in a segment
    the appender has moved on from."""
    engine = _genesis(run_root)
    engine.inject(Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": "1"}))
    asyncio.run(engine.run_until_quiescent(T0))
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    opened = _resume(run_root, C2_JIL)
    opened.inject(Event(at=T0, kind="SET_GLOBAL", payload={"name": "H", "value": "2"}))
    asyncio.run(opened.run_until_quiescent(T0))
    return opened


def test_pr49_the_backfill_spans_segments_after_a_boundary(tmp_path: Path) -> None:
    """DL-135: `since` cuts into the ESTATE's records, not into the active
    segment's.

    A subscriber that resumes with a cursor taken under C1 was answered
    with C2's records alone and no sign that anything came before them --
    the gap it was resuming to avoid. I2 makes the index estate-wide, so
    the cursor already meant one thing across the lineage and only the
    reader had to widen."""
    run_root = tmp_path / "run"
    opened = _two_periods(run_root)
    try:
        streamed = _subscribed(opened, run_root, since=0)
    finally:
        _close(opened)
    assert streamed[0] == {"ok": True, "subscribed": True}
    body = streamed[1:]
    assert [r["period_id"] for r in body if r.get("rec") == "segment"] == [1, 2]
    # both admitted inputs, each exactly once, in estate index order
    globals_set = [r for r in body if r.get("kind") == "SET_GLOBAL"]
    assert [r["payload"]["name"] for r in globals_set] == ["G", "H"]
    assert [r["seq"] for r in globals_set] == sorted(r["seq"] for r in globals_set)
    # and the seal that ends period 1 is on the stream between them
    assert any(r.get("rec") == "seal" for r in body)


def test_pr49_a_cursor_inside_the_closed_segment_still_cuts_positionally(
    tmp_path: Path,
) -> None:
    """The counterpart: a cursor INSIDE the closed segment delivers the
    rest of that segment and then the next one.

    Without it the test above would pass on a build that ignored `since`
    and always sent everything."""
    run_root = tmp_path / "run"
    opened = _two_periods(run_root)
    try:
        under_c1 = [
            record["seq"]
            for record in read_journal(wal_path(run_root, 1))
            if record.get("kind") == "SET_GLOBAL"
        ]
        streamed = _subscribed(opened, run_root, since=under_c1[0])
    finally:
        _close(opened)
    body = streamed[1:]
    assert not any(r.get("gap") for r in body)
    # the cursor's own record is not re-delivered; period 2's is
    assert [r["payload"]["name"] for r in body if r.get("kind") == "SET_GLOBAL"] == ["H"]
    assert [r["period_id"] for r in body if r.get("rec") == "segment"] == [2]


def test_pr49_a_cursor_below_the_earliest_retained_record_gets_the_gap_marker(
    tmp_path: Path,
) -> None:
    """ss11: a client asking for an index below the earliest retained
    record receives `{"gap": true, "earliest_retained": <index>}`.

    A physical roll is the reachable case: the new root holds the seal it
    opened from and none of the closing period's WAL, by design, so a
    subscriber resuming there with a C1 cursor cannot be given what it
    asked for. Silence would read as "nothing happened between your cursor
    and the first line you got"."""
    from dsl41.estate import roll_into_root

    root_a = tmp_path / "a"
    engine = _genesis(root_a)
    engine.inject(Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": "1"}))
    asyncio.run(engine.run_until_quiescent(T0))
    asyncio.run(_seal(engine, _request(engine, _stage(root_a, C2_JIL))))
    _close(engine)
    anchor_dir = default_anchor_dir(root_a)
    audit_period(root_a, 1, anchor=EstateAnchor(anchor_dir))
    catalog, _ = _catalog(C2_JIL)
    root_b = tmp_path / "b"
    roll_into_root(root_b, anchor_dir=anchor_dir, catalog_of=lambda _r, _m: catalog)
    rolled = asyncio.run(
        resume_run(
            catalog,
            root_b,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            anchor_dir=anchor_dir,
        )
    )
    try:
        assert wal_segments(root_b) == [2]  # the roll imports no earlier WAL
        first_index = read_journal(wal_path(root_b, 2))[0]["first_index"]
        streamed = _subscribed(rolled, root_b, since=0)
    finally:
        _close(rolled)
    assert streamed[1] == {"gap": True, "earliest_retained": first_index}
    assert [r["period_id"] for r in streamed[2:] if r.get("rec") == "segment"] == [2]
    # the boundary is pinned from BOTH sides, one index apart: a cursor at
    # `first_index - 1` is contiguous with what the root holds and gets no
    # marker; one at `first_index - 2` is missing exactly one record and
    # does. A rule off by one either way reds here
    assert not any(r.get("gap") for r in _subscribed(rolled, root_b, since=first_index - 1))
    assert any(r.get("gap") for r in _subscribed(rolled, root_b, since=first_index - 2))
    # ... and a cursor below index 1 is not a gap at all: index 1 is the
    # first there ever is, so nothing was lost below it
    fresh = tmp_path / "c"
    plain = _genesis(fresh)
    try:
        assert not any(r.get("gap") for r in _subscribed(plain, fresh, since=-1))
    finally:
        _close(plain)


def test_pr49_a_cursor_inside_the_live_period_reads_one_segment(tmp_path: Path) -> None:
    """The backfill reads BACKWARDS and stops at the first segment holding
    a record at or below the cursor.

    Everything older sits before the positional cut, so reading it would
    be work whose whole result is discarded -- and on an estate that has
    crossed many boundaries, discarded work on the single-writer loop. It
    is observable: a closed segment this root could not parse at all does
    not stop a subscriber resuming inside the live period."""
    run_root = tmp_path / "run"
    opened = _two_periods(run_root)
    try:
        under_c2 = [
            record["seq"]
            for record in read_journal(wal_path(run_root, 2))
            if record.get("kind") == "SET_GLOBAL"
        ]
        wal_path(run_root, 1).write_bytes(b"{not a record at all\n")
        streamed = _subscribed(opened, run_root, since=under_c2[0])
        assert streamed[0] == {"ok": True, "subscribed": True}
        assert not any(r.get("ok") is False for r in streamed[1:])
        assert not any(r.get("gap") for r in streamed[1:])
        # and the counterpart, from the same damaged root: a cursor that
        # DOES need the older segment meets it and is refused
        refused = _subscribed(opened, run_root, since=0)
        assert refused[1]["ok"] is False
    finally:
        _close(opened)


def test_a_foreign_file_under_wal_refuses_the_backfill_on_the_stream(tmp_path: Path) -> None:
    """The backfill now spans segments, so it can meet a file this estate
    did not write.

    The ack has already gone by then, so the refusal goes ON THE STREAM.
    A handler that raised here would hang the client up with no answer at
    all, which is the one thing control-protocol ss2 says must not
    happen."""
    run_root = tmp_path / "run"
    opened = _two_periods(run_root)
    try:
        (run_root / "wal" / "notes.txt").write_text("an operator's note\n")
        streamed = _subscribed(opened, run_root, since=0)
        assert streamed[0] == {"ok": True, "subscribed": True}
        assert streamed[1]["ok"] is False and "not a segment file" in streamed[1]["error"]
        assert len(streamed) == 2  # refused, not refused-and-then-streamed
        # the counterpart: remove it and the same subscription works
        (run_root / "wal" / "notes.txt").unlink()
        assert [
            r["period_id"]
            for r in _subscribed(opened, run_root, since=0)[1:]
            if r.get("rec") == "segment"
        ] == [1, 2]
    finally:
        _close(opened)


def test_a_closed_segment_with_a_torn_tail_refuses_rather_than_skipping_records(
    tmp_path: Path,
) -> None:
    """`read_journal` tolerates a torn FINAL line, which is right for the
    file an appender is still appending to and wrong for a closed one.

    In a closed segment a tolerated tail is a hole in the MIDDLE of the
    concatenation, and the subscriber would be handed a stream that skips
    records without being told. Every segment but the last ends in a
    `seal`, so that is what is checked."""
    run_root = tmp_path / "run"
    opened = _two_periods(run_root)
    try:
        segment = wal_path(run_root, 1)
        intact = segment.read_bytes()
        lines = intact.splitlines()
        assert json.loads(lines[-1])["rec"] == "seal"
        segment.write_bytes(b"\n".join(lines[:-1]) + b"\n" + lines[-1][:20])
        streamed = _subscribed(opened, run_root, since=0)
        assert streamed[1]["ok"] is False
        assert "a backfill across it would skip records silently" in streamed[1]["error"]
        # PRECEDENCE, pinned (DL-142 review): the tail is decided BEFORE
        # identity, sequence and contiguity. A segment that is BOTH torn
        # and mis-identified names the tail -- the fault that makes the
        # stream a lie -- and not the one a later check happens to reach
        torn = segment.read_bytes()
        both = [json.loads(line) for line in torn.splitlines()[:-1]]
        both[0] = {**both[0], "estate_id": "e-stranger"}
        from dsl41.canon import canonical_bytes

        segment.write_bytes(b"".join(canonical_bytes(record) + b"\n" for record in both))
        two_faults = _subscribed(opened, run_root, since=0)
        assert two_faults[1]["ok"] is False
        assert "a backfill across it would skip records silently" in two_faults[1]["error"]
        assert "stranger's segment" not in two_faults[1]["error"]
        segment.write_bytes(intact)  # the gate, not the machinery
        assert any(r.get("rec") == "seal" for r in _subscribed(opened, run_root, since=0))
    finally:
        _close(opened)


# ------------------------- ss2.1 the profile the opener is held to (DL-151)


#: a box and its one member: the only shape that can put a live BOX row
#: behind an `executions` entry, which is what ss3.5's CMD-or-FW half is
#: about
BOX_JIL = "insert_job: bx\njob_type: b\n\ninsert_job: a\njob_type: c\ncommand: x\nbox_name: bx\n"


def _resume_wired(
    run_root: Path,
    text: str,
    *,
    default_tz: str | None = None,
    declared: RuntimeProfile | None = None,
):
    """`_resume` with the two things the profile gate reads: a real
    scheduler (so `default_tz` is WIRED rather than inherited) and the
    launcher's declared profile."""
    catalog, _ = _catalog(text)
    return asyncio.run(
        resume_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=Scheduler(catalog, start=T0, default_tz=default_tz),
            declared=declared,
        )
    )


def _committed_boundary_over(run_root: Path, profile: RuntimeProfile):
    """A root whose newest boundary is COMMITTED and not yet opened, with
    period 2 pinned to `profile`."""
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL, profile=profile)
    boundary = asyncio.run(_seal(engine, _request(engine, staged)))
    _close(engine)
    return boundary


def test_pr22b_a_committed_profile_change_refuses_before_it_writes_the_segment(
    tmp_path: Path,
) -> None:
    """ss2.1: the period a resume OPENS INTO is the committed boundary's,
    so the wiring is held to N+1's pin -- and the refusal has to come
    BEFORE the successor's segment is written.

    It did not. `_open_from_seal` wrote `wal/000002.jsonl` and moved the
    head, and only the gate at the end of the ladder refused; the SECOND
    try with the same wrong wiring then met an ordinary open period and
    succeeded. A boundary that changes the profile must refuse while it is
    still a boundary (DL-151)."""
    run_root = tmp_path / "run"
    _committed_boundary_over(run_root, RuntimeProfile(default_tz="Europe/Zurich"))

    with pytest.raises(EngineError, match="runtime-profile mismatch on default_tz"):
        _resume_wired(run_root, C2_JIL)  # C1's wiring: UTC
    assert wal_segments(run_root) == [1]  # nothing written
    stored = EstateAnchor(default_anchor_dir(run_root)).read()
    assert stored is not None and isinstance(stored.head, ClosedHead)  # the head never moved

    with pytest.raises(EngineError, match="runtime-profile mismatch on default_tz"):
        _resume_wired(run_root, C2_JIL)  # ...and the second try refuses too
    assert wal_segments(run_root) == [1]

    opened = _resume_wired(run_root, C2_JIL, default_tz="Europe/Zurich")
    assert wal_segments(run_root) == [1, 2]
    _close(opened)


def test_pr22b_the_opener_is_held_to_the_staged_machine_identity(tmp_path: Path) -> None:
    """ss2.1: `as_machine` and `machine_policy` change what this runner
    answers to, so they are part of a period's identity -- but no wired
    object can report them (they act in preflight, over the catalog).

    They therefore INHERITED the pin, which made them unable to disagree
    with it: a boundary that staged a new machine identity opened silently
    under the OLD one, the process still answering to the names it was
    started with while the manifest pinned others. The launcher now
    declares them and is held to them (DL-151)."""
    run_root = tmp_path / "run"
    _committed_boundary_over(run_root, RuntimeProfile(as_machine=("boxa",)))

    with pytest.raises(EngineError, match="runtime-profile mismatch on as_machine"):
        _resume_wired(run_root, C2_JIL, declared=RuntimeProfile())
    assert wal_segments(run_root) == [1]

    opened = _resume_wired(run_root, C2_JIL, declared=RuntimeProfile(as_machine=("boxa",)))
    assert read_period_manifest(run_root, 2).runtime_profile.as_machine == ("boxa",)  # type: ignore[union-attr]
    _close(opened)


def test_pr22b_a_caller_that_declares_nothing_still_inherits_the_pin(tmp_path: Path) -> None:
    """The control, and the reason `declared` is a parameter rather than a
    new rule: an embedder that says nothing about a machine identity has
    said nothing to hold to the pin, and inherits it exactly as before."""
    run_root = tmp_path / "run"
    _committed_boundary_over(run_root, RuntimeProfile(as_machine=("boxa",)))
    opened = _resume_wired(run_root, C2_JIL)  # no `declared`
    assert wal_segments(run_root) == [1, 2]
    _close(opened)


def _forge_execution(run_root: Path, period_id: int, job: str) -> None:
    """Put a live row and an `executions` entry for `job` into the stored
    sidecar, through the model, and re-point BOTH things that name it: the
    `seal` record in the WAL and the anchor head.

    So the artifact stays self-consistent, the lineage still names it, and
    only the catalog-aware half of the ss3.5 join has anything to object
    to -- which is the whole point of the test below."""
    from dsl41.boundary import seal_record
    from dsl41.seal import Seal

    seal = read_seal(run_root, period_id)
    payload = seal.to_payload()
    payload["state"]["jobs"][job] = {
        **payload["state"]["jobs"][job],
        "status": "RUNNING",
        "run_number": 1,
    }
    payload["executions"] = [
        {
            "kind": "bound",
            "job": job,
            "run_number": 1,
            "effect_id": "e-1",
            "index": 1,
            "run_id": "11111111-1111-4111-8111-111111111111",
            "executor_id": "local",
            "generation": 0,
            "run_dir": f"runs/{job}.1",
        }
    ]
    forged = Seal(**payload)
    seal_path(run_root, period_id).write_bytes(forged.to_bytes())

    path = active_wal(run_root)
    lines = path.read_text().splitlines()
    record = json.loads(lines[-1])
    assert record["rec"] == "seal"
    named = seal_record(forged)
    path.write_text(
        "\n".join([*lines[:-1], json.dumps({**record, **named}, sort_keys=True)]) + "\n"
    )

    anchor = EstateAnchor(default_anchor_dir(run_root))
    anchor.acquire()
    try:
        stored = anchor.require()
        assert isinstance(stored.head, ClosedHead)
        anchor.write(
            stored.model_copy(
                update={"head": stored.head.model_copy(update={"seal_digest": forged.digest})}
            )
        )
    finally:
        anchor.release()


def test_pr22a_an_execution_entry_behind_a_box_row_is_refused_at_the_loader(
    tmp_path: Path,
) -> None:
    """ss3.5: every `executions` entry has a RUNNING or STARTING **CMD or
    FW** row. A RUNNING box has no adapter, no effect and no entry -- boxes
    are deliberately outside `dispatchable`.

    The seal's own join could not ask it: knowing a job's TYPE needs the
    catalog the period opens with, so `_check_join` deferred this half to
    "the loader that holds C2" -- and no loader did it. A forged or
    corrupted sidecar naming a live BOX row passed every gate on the way in
    (DL-151). The counter-case below is the same forgery over the box's
    MEMBER, which is a legal live execution and must still open."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root, text=BOX_JIL)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, BOX_JIL))))
    _close(engine)
    _forge_execution(run_root, 1, "bx")

    with pytest.raises(EngineError, match="names box 'bx'"):
        _resume_wired(run_root, BOX_JIL)
    assert wal_segments(run_root) == [1]  # refused before the successor is written
    stored = EstateAnchor(default_anchor_dir(run_root)).read()
    assert stored is not None and isinstance(stored.head, ClosedHead)  # ...and unclaimed


def test_pr22a_the_same_entry_behind_a_dispatchable_row_still_opens(tmp_path: Path) -> None:
    """The counter-case: an entry behind the MEMBER is a legal live
    execution across a boundary, and the check must not refuse it."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root, text=BOX_JIL, profile=DETACHED)
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, BOX_JIL, profile=DETACHED))))
    _close(engine)
    _forge_execution(run_root, 1, "a")

    opened = _resume_wired(run_root, BOX_JIL)
    assert wal_segments(run_root) == [1, 2]
    _close(opened)


def test_sem35_every_replay_of_a_map_only_zone_reads_the_periods_own_table(
    tmp_path: Path,
) -> None:
    """SEM-35/DL-62: a `ujo_timezones` name is site-local, and the only
    record of the table that resolves it is the period's own
    `RuntimeProfile`. Neither the catalog nor the WAL carries one.

    So every reader that replays the log has to read the pin, or it refuses
    the very log the engine wrote: the engine resolves `timezone: ujo_ny`
    through the scheduler's table, and audit, the history fold and the
    journal narration each build their own Oracle. Wiring one and not the
    others is a divergence with a green engine (DL-151)."""
    from dsl41.runner_history import replay_trace

    aliases = {"ujo_ny": "America/New_York"}
    text = (
        "insert_job: rw\njob_type: c\ncommand: x\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        'run_window: "00:00-23:59"\ntimezone: ujo_ny\n'
    )
    run_root = tmp_path / "run"
    catalog, sources = _catalog(text)
    profile = RuntimeProfile(tz_aliases=aliases)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=profile,
        state_machine_version=STATE_MACHINE_VERSION,
    )
    engine = start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        # a COMPLETING adapter: the boundary below refuses over live work,
        # and what this test needs from the run is the zone lookup a start
        # performs, not a job left running across it
        adapters={"CMD": FakeAdapter(default=(0.0, 0))},
        scheduler=Scheduler(catalog, start=T0, tz_aliases=aliases),
        staged=staged,
    )
    # the start is what asks for the zone: the window is read in the job's
    # own, so `_job_tz` runs and an oracle without the table raises here
    engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "rw"}))
    asyncio.run(engine.run_until_quiescent(T0))
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, text, profile=profile))))
    _close(engine)

    records = read_journal(wal_path(run_root, 1))
    replay_trace(run_root, records, catalog)  # the history fold
    audit_period(run_root, 1, anchor=EstateAnchor(default_anchor_dir(run_root)))  # audit
