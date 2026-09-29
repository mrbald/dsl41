"""A restoration drill: backup, delete, restore, re-open (period-model ss1.3,
ss7, ss11a, ss12; deployment-runbook ss2, ss2b; DL-133..135, DL-144, DL-219).

This is not a new subsystem. It is a rehearsal over the real machinery
`examples/nightbank` already exercises in `test_nightbank_boundary.py`: a
detached night with a real supervisor, an offline seal, a physical roll, and
an archived period. What is new here is the file-system half nothing else in
this repo drives end to end -- copy the estate's artifacts out, delete the
originals, put them back at the SAME absolute path, and prove a fresh reader
built from nothing but the restored files can pick the lineage back up.

See `docs/deployment-runbook.md` ss2b for the quiescence order (engine,
then offline seal while the detached run's supervisor is still up, then
the supervisor, only then back up) and the path-equality constraint (a
restore must land at the SAME absolute path the backup came from, or
every estate-wide reader refuses it as a missing registered root).

One estate, one fixed absolute path (`short_root`, conftest.py's
AF_UNIX-safe `tempfile.mkdtemp` under `/tmp`): a period runs DETACHED
with one real command under a real supervisor, is sealed offline, and is
attested; a physical roll opens period 2 into a second root under the
same base; period 2 is sealed and attested too, so the estate carries
one DERIVATION-verified period beside the one ATTESTATION-verified
period the archive leaves behind. The estate's own JIL
files are copied under the base first and loaded from there throughout, so
they are part of what gets backed up, deleted and restored too -- not read
from the repo checkout, which a restore has no reason to depend on. Backup,
delete, restore, and every check after that runs through fresh CLI
invocations and a fresh `resume_run` -- nothing held over from before the
delete.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

import test_nightbank_boundary as tnb
from dsl41.attest import ATTESTATION_VERIFIED, DERIVATION_VERIFIED, verified_tier
from dsl41.boundary import EstateAnchor, OpenHead, default_anchor_dir
from dsl41.period import wal_path
from test_nightbank_boundary import (
    _drive,
    _invoke,
    _open_in_place,
    _roll,
    _seal_offline,
    _sendevent,
    _start_detached_night,
    _stop_engine,
    _wait_for_evidence,
)
from test_runner_supervisor import _kill_group, wait_for

#: the one detached job the drill actually runs: a short, unconditioned box
#: member (period-model ss8's supervisor proof does not care that it sits in
#: a box) with an 8-second `fakework` sleep, coupled to ss14 B's own
#: `--sleep` floors the way every job `test_nightbank_boundary.py` names
#: is -- left alone, per the review that asked for this note.
JOB = "AMER_INV_MACROS_C"

#: how long real subprocess work is allowed before the drill calls it
#: wedged rather than slow -- generous beside the 8-second job so a loaded
#: CI host is not what fails this.
_JOB_TIMEOUT_S = 30.0

#: every status the job's real wrapper can reach that ends the wait -- not
#: only the one the happy path expects. A set that only named SUCCESS would
#: spin out the full timeout on a signaled or failed run instead of failing
#: fast with the status attached.
_TERMINAL = ("SUCCESS", "FAILURE", "TERMINATED")


def _copy_estate_locally(base: Path) -> list[Path]:
    """Copies of the "small" catalog's JIL files under `base`, so the
    drill's estate is part of what it backs up and restores rather than
    the repo checkout `examples/nightbank` -- a restore has no business
    depending on that."""
    estate_dir = base / "estate"
    estate_dir.mkdir()
    copies = []
    for source in tnb.SMALL_FILES:
        target = estate_dir / Path(source).name
        shutil.copy2(source, target)
        copies.append(target)
    return sorted(copies)


async def _build_root_a(base: Path):
    """Period 1: a detached night with one real command run to a real
    terminal status under a real supervisor, then the engine alone
    stopped -- the supervisor, and the pid and sock files it holds,
    outlive it on purpose (runner-design ss6a).

    One `asyncio.run` call for genesis, the dispatch and the drain to
    quiescence -- `SupervisorClient`'s connection is bound to the loop that
    made it, and a later `asyncio.run` for the next step would run it over
    a closed one (this is what made an earlier version of this drill hang
    with the job stuck RUNNING and nothing in the supervisor's LIST)."""
    night = await _start_detached_night(base)
    try:
        _sendevent(night, "FORCE_STARTJOB", job=JOB)
        await _drive(night)
        await _wait_for_evidence(
            night,
            lambda: night.engine.oracle.store.runtime(JOB).status in _TERMINAL,
            f"{JOB} terminal",
            _JOB_TIMEOUT_S,
        )
        status = night.engine.oracle.store.runtime(JOB).status
        assert status == "SUCCESS", f"{JOB} did not finish cleanly: {status}"
        await _stop_engine(night)
    except BaseException:
        _kill_group(night.run_root)
        raise
    return night.run_root


def test_the_restoration_drill(short_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build a closed, quiescent, two-root lineage with a physical roll and
    an archived period; back it up; destroy the originals; restore at the
    same absolute path; read it with nothing carried over from before the
    delete. Three refusals along the way: no anchor, a root the registry
    names but the disk does not have, and the whole lineage restored at a
    different absolute path (still a missing-registered-root refusal, not a
    claim-digest one).

    `short_root` IS the drill's one fixed absolute path: every root, the
    anchor, the copied JIL inputs, the night's properties, and the backup
    itself all live under it for the whole test."""
    base = short_root
    local_jil = _copy_estate_locally(base)
    monkeypatch.setattr(tnb, "SMALL_FILES", local_jil)

    run_root = asyncio.run(_build_root_a(base))
    props = base / "night.properties"
    anchor_dir = default_anchor_dir(run_root)

    try:
        # ---- quiescence: the engine is down; the supervisor is not (yet) ----
        still_up = _invoke("supervise", "list", "--run-root", str(run_root))
        assert still_up.exit_code == 0, still_up.output  # the engine-stop left this writer

        # quiescence order: deployment-runbook.md ss2b
        sealed = _seal_offline(run_root, props, "--claimed-actor", "drill@nightbank")
        assert sealed.exit_code == 0, sealed.output

        down = _invoke("supervise", "shutdown", "--run-root", str(run_root))
        assert down.exit_code == 0, down.output
    except BaseException:
        _kill_group(run_root)
        raise

    # quiescence order: deployment-runbook.md ss2b
    gone = _invoke("supervise", "list", "--run-root", str(run_root))
    assert gone.exit_code != 0
    wait_for(lambda: not (run_root / "supervisor.pid").exists())
    wait_for(lambda: not (run_root / "supervisor.sock").exists())

    assert _invoke("audit", "--run-root", str(run_root)).exit_code == 0

    # ---- the physical roll ----
    rolled_root = base / "roll"
    _roll(rolled_root, anchor_dir, props)

    sealed_b = _seal_offline(
        rolled_root, props, "--claimed-actor", "drill@nightbank", "--estate-anchor", str(anchor_dir)
    )
    assert sealed_b.exit_code == 0, sealed_b.output
    audited_b = _invoke("audit", "--run-root", str(rolled_root), "--estate-anchor", str(anchor_dir))
    assert audited_b.exit_code == 0, audited_b.output

    assert verified_tier(run_root, 1) == DERIVATION_VERIFIED
    assert verified_tier(rolled_root, 2) == DERIVATION_VERIFIED

    # ---- archive period 1: attested, and covered by period 2's later
    # ---- checkpoint (period-model ss12a) held in the SUCCESSOR root ----
    kept_wal = wal_path(run_root, 1).read_bytes()  # for the archive-is-irreversible check below
    tombstones = _invoke("estate", "prune", "--run-root", str(run_root), "--tombstones")
    assert tombstones.exit_code == 0, tombstones.output
    archived = _invoke("estate", "prune", "--run-root", str(run_root), "--archive-inputs")
    assert archived.exit_code == 0, archived.output
    assert verified_tier(run_root, 1) == ATTESTATION_VERIFIED
    assert not wal_path(run_root, 1).exists()

    # ============================= the backup =============================
    # kept under `base` itself: the fixture's own teardown is what removes
    # it, so nothing this test creates leaks beside the tmpdir.
    backup_dir = base / "backup"
    backup_dir.mkdir()
    shutil.copytree(anchor_dir, backup_dir / "anchor")
    shutil.copytree(run_root, backup_dir / "root-a")
    shutil.copytree(rolled_root, backup_dir / "root-b")
    shutil.copytree(base / "estate", backup_dir / "estate")
    shutil.copy2(props, backup_dir / "night.properties")

    # ============================ destroy the originals ============================
    # everything under `base` except the backup itself -- not a hand-picked
    # list, so a restore that leaves something out is a restore this test
    # would actually notice, and the surviving `data`/`logs`/`profile.env`
    # directories `prepare_night` also wrote are gone too: the checks below
    # never restore them, and the drill still passes, which is the proof
    # that the restore does not depend on them (deployment-runbook ss2b's
    # "what this does not prove" -- a job's own side effects are outside
    # this inventory).
    for child in base.iterdir():
        if child == backup_dir:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    assert not anchor_dir.exists() and not run_root.exists() and not rolled_root.exists()

    # ---- negative: restored roots, no restored anchor ----
    shutil.copytree(backup_dir / "root-a", run_root)
    shutil.copytree(backup_dir / "root-b", rolled_root)
    no_anchor = _invoke("audit", "--estate-anchor", str(anchor_dir))
    assert no_anchor.exit_code == 2, no_anchor.output
    assert "no anchor" in no_anchor.output
    shutil.rmtree(run_root)
    shutil.rmtree(rolled_root)

    # ---- negative: restored anchor and root A, root B still missing ----
    shutil.copytree(backup_dir / "anchor", anchor_dir)
    shutil.copytree(backup_dir / "root-a", run_root)
    missing_root = _invoke("audit", "--estate-anchor", str(anchor_dir))
    assert missing_root.exit_code == 2, missing_root.output
    assert f"registry root {rolled_root.resolve()} is missing" in missing_root.output
    shutil.rmtree(anchor_dir)
    shutil.rmtree(run_root)

    # ---- negative: the WHOLE lineage restored at a DIFFERENT absolute
    # ---- path (path-equality constraint: deployment-runbook.md ss2b) ----
    wrong_base = base / "wrong-path"
    wrong_base.mkdir()
    shutil.copytree(backup_dir / "anchor", wrong_base / "engine.anchor")
    shutil.copytree(backup_dir / "root-a", wrong_base / "engine")
    shutil.copytree(backup_dir / "root-b", wrong_base / "roll")
    wrong_path_read = _invoke("audit", "--estate-anchor", str(wrong_base / "engine.anchor"))
    assert wrong_path_read.exit_code == 2, wrong_path_read.output
    assert f"registry root {run_root.resolve()} is missing" in wrong_path_read.output
    shutil.rmtree(wrong_base)

    # ============================ the full restore ============================
    shutil.copytree(backup_dir / "anchor", anchor_dir)
    shutil.copytree(backup_dir / "root-a", run_root)
    shutil.copytree(backup_dir / "root-b", rolled_root)
    shutil.copytree(backup_dir / "estate", base / "estate")
    shutil.copy2(backup_dir / "night.properties", props)
    assert anchor_dir.exists() and run_root.exists() and rolled_root.exists()
    assert not (base / "data").exists()  # a job's own side effects: not restored, not needed

    # the restore actually put the JIL back -- SMALL_FILES was patched once,
    # above, and every helper below still reads through it
    assert any((base / "estate").glob("*.jil"))

    # ---- fresh readers only: nothing above holds an engine, a client or a
    # ---- catalog object from before the delete ----
    read_back = _invoke("audit", "--estate-anchor", str(anchor_dir))
    assert read_back.exit_code == 0, read_back.output
    assert (
        f"period 1 in {run_root.resolve()} inputs archived, {ATTESTATION_VERIFIED}:"
        in read_back.output
    )
    assert (
        f"period 2 in {rolled_root.resolve()} attested, {DERIVATION_VERIFIED}:" in read_back.output
    )
    assert verified_tier(run_root, 1) == ATTESTATION_VERIFIED
    assert verified_tier(rolled_root, 2) == DERIVATION_VERIFIED

    verify_b = _invoke("verify", "--run-root", str(rolled_root), "--period", "2")
    assert verify_b.exit_code == 0, verify_b.output

    # ---- the archive is irreversible: restoring the WAL beside the
    # ---- receipt does not move period 1 back to derivation-verified
    # ---- (period-model ss12a; PR-55) ----
    wal_path(run_root, 1).write_bytes(kept_wal)
    assert verified_tier(run_root, 1) == ATTESTATION_VERIFIED
    still_archived = _invoke("audit", "--run-root", str(run_root), "--period", "1")
    assert still_archived.exit_code == 0
    assert ATTESTATION_VERIFIED in still_archived.output
    assert DERIVATION_VERIFIED not in still_archived.output
    wal_path(run_root, 1).unlink()  # put the restored root back the way the backup left it

    # ---- open the next synthetic period once, from the restored files
    # ---- alone ----
    _open_in_place(rolled_root, props, anchor_dir=anchor_dir)
    assert wal_path(rolled_root, 3).exists()
    head = EstateAnchor(anchor_dir).read()
    assert head is not None
    assert isinstance(head.head, OpenHead)
    assert head.head.period_id == 3
    assert Path(head.head.root).resolve() == rolled_root.resolve()
