"""Branch tests for src/dsl41/attest.py: the archive receipt's bindings, the
attestation's version stamp, the refusals of audit and re-derivation, and the
diagnostic that names the interpreter to install.

The estate setup is test_estate.py's own: a native period-1 root sealed offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from dsl41.attest import (
    Attestation,
    audit_period,
    read_attestation,
    rederive_seal,
    verify_archive_receipt,
)
from dsl41.boundary import read_seal
from dsl41.canon import canonical_bytes
from dsl41.period import (
    ARCHIVE_CLASS,
    ArchiveReceipt,
    archivable_names,
    attestation_path,
    period_dir,
    read_sentinel,
    seal_path,
    wal_path,
    write_archive_receipt,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_journal import read_journal

from test_estate import (
    _estate,
    _invoke,
    _native_root,
    _pin_version,
    _roll,
    _seal_offline,
)


def _sealed_root(tmp_path: Path) -> tuple[Path, Path]:
    """`(run_root, c2)`: a native period 1, sealed offline."""
    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    assert _seal_offline(run_root, c2).exit_code == 0
    return run_root, c2


def _receipt(estate_id: str, period_id: int = 1) -> ArchiveReceipt:
    return ArchiveReceipt(
        estate_id=estate_id,
        period_id=period_id,
        seal_digest="sha256:" + "11" * 32,
        attestation_digest="sha256:" + "22" * 32,
        chain_through_period=period_id,
        retention_class=ARCHIVE_CLASS,
        archived=(archivable_names(period_id)[0],),
        archived_at="2026-08-21T10:00:00.000000",
        dsl41_version="1.2.3",
    )


# ------------------------------------------------- the archive receipt's bindings


def test_a_receipt_in_a_root_with_no_sentinel_proves_nothing(tmp_path: Path) -> None:
    root = tmp_path / "bare"
    write_archive_receipt(root, _receipt("estate-x"))
    assert read_sentinel(root) is None
    with pytest.raises(EngineError, match="no `.*` sentinel"):
        verify_archive_receipt(root, 1)


def test_a_receipt_whose_sidecar_is_gone_is_loss_not_an_archive(tmp_path: Path) -> None:
    c1, _, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    sentinel = read_sentinel(run_root)
    assert sentinel is not None
    write_archive_receipt(run_root, _receipt(sentinel.estate_id))
    assert not seal_path(run_root, 1).exists()
    with pytest.raises(EngineError, match="its sidecar is gone"):
        verify_archive_receipt(run_root, 1)


def test_no_receipt_is_a_fact_not_a_refusal(tmp_path: Path) -> None:
    run_root, _ = _sealed_root(tmp_path)
    assert verify_archive_receipt(run_root, 1) is None


# ------------------------------------------------------------ the attestation


def test_an_attestation_with_no_producing_version_refuses() -> None:
    fields = {
        "seal_digest": "sha256:" + "11" * 32,
        "period_id": 1,
        "chain_through_period": 1,
        "prev_attestation_digest": None,
        "state_machine_version": 1,
        "audited_at": "2026-08-20T00:00:00.000000",
    }
    with pytest.raises(ValidationError, match="dsl41_version is empty"):
        Attestation(dsl41_version="", **fields)
    assert Attestation(dsl41_version="1.2.3", **fields).dsl41_version == "1.2.3"


# ----------------------------------------- audit and re-derivation on a rolled root


def _rolled_root(tmp_path: Path) -> Path:
    """A root that holds the imported seal of period 1 and none of its evidence."""
    c1, c2, _ = _estate(tmp_path / "estate")
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    from dsl41.boundary import default_anchor_dir

    _roll(root_b, default_anchor_dir(root_a), c2)
    assert seal_path(root_b, 1).is_file() and not wal_path(root_b, 1).exists()
    return root_b


def test_audit_of_an_imported_seal_says_the_root_never_held_its_evidence(tmp_path: Path) -> None:
    root_b = _rolled_root(tmp_path)
    with pytest.raises(EngineError, match="is not in this root") as caught:
        audit_period(root_b, 1)
    assert "ran period" not in str(caught.value)  # not the loss wording


def test_re_derivation_on_an_imported_seal_says_the_wal_is_not_in_this_root(
    tmp_path: Path,
) -> None:
    root_b = _rolled_root(tmp_path)
    with pytest.raises(EngineError, match="WAL is not in this root") as caught:
        rederive_seal(root_b, 1)
    assert "ARCHIVED" not in str(caught.value)  # not the archive wording


# ------------------------------------------------ the segment re-derivation reads


def test_re_derivation_of_a_period_still_open_refuses(tmp_path: Path) -> None:
    c1, _, _ = _estate(tmp_path / "estate")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    with pytest.raises(EngineError, match="no `seal` record"):
        rederive_seal(run_root, 1)


def test_re_derivation_without_the_closing_manifest_refuses(tmp_path: Path) -> None:
    run_root, _ = _sealed_root(tmp_path)
    assert rederive_seal(run_root, 1).digest == read_seal(run_root, 1).digest
    manifest = period_dir(run_root, 1) / "manifest.json"
    manifest.unlink()
    with pytest.raises(EngineError, match="cannot invent one"):
        rederive_seal(run_root, 1)


def test_re_derivation_of_a_segment_that_admitted_no_input_refuses(tmp_path: Path) -> None:
    run_root, _ = _sealed_root(tmp_path)
    path = wal_path(run_root, 1)
    records = read_journal(path)
    kept = [r for r in records if r.get("rec") not in ("advance", "decision")]
    assert len(kept) < len(records)
    path.write_bytes(b"".join(canonical_bytes(record) + b"\n" for record in kept))
    with pytest.raises(EngineError, match="the segment admitted no input"):
        rederive_seal(run_root, 1)


# ------------------------------------- the version a refused audit tells you to install


def _foreign_version_root(tmp_path: Path) -> Path:
    run_root, _ = _sealed_root(tmp_path)
    _pin_version(run_root, read_seal(run_root, 1).state_machine_version + 1)
    return run_root


def test_the_version_refusal_names_the_attestations_interpreter_when_there_is_one(
    tmp_path: Path,
) -> None:
    """The attestation carries a version no leader record and no running binary has,
    so only the attestation arm can name it."""
    run_root, _ = _sealed_root(tmp_path)
    assert _invoke("audit", "--run-root", str(run_root)).exit_code == 0
    stamped = read_attestation(run_root, 1)
    assert stamped is not None
    older = stamped.model_copy(update={"dsl41_version": "9.9.9"})
    attestation_path(run_root, 1).write_bytes(older.to_bytes())
    leaders = [r for r in read_journal(wal_path(run_root, 1)) if r.get("rec") == "leader"]
    assert all(r["dsl41_version"] != "9.9.9" for r in leaders)
    _pin_version(run_root, read_seal(run_root, 1).state_machine_version + 1)
    with pytest.raises(EngineError, match="install dsl41 9.9.9 and"):
        audit_period(run_root, 1)


def test_the_version_refusal_falls_back_to_the_leader_records_when_the_attestation_is_unreadable(
    tmp_path: Path,
) -> None:
    run_root, _ = _sealed_root(tmp_path)
    attestation_path(run_root, 1).write_bytes(b"{not an attestation\n")
    leaders = [r for r in read_journal(wal_path(run_root, 1)) if r.get("rec") == "leader"]
    wrote = str(leaders[-1]["dsl41_version"])
    _pin_version(run_root, read_seal(run_root, 1).state_machine_version + 1)
    with pytest.raises(EngineError, match=f"install dsl41 {wrote} and"):
        audit_period(run_root, 1)


def test_the_version_refusal_still_refuses_when_the_wal_cannot_be_read_either(
    tmp_path: Path,
) -> None:
    run_root = _foreign_version_root(tmp_path)
    wal_path(run_root, 1).write_bytes(b"{torn\n{interior line\n")
    with pytest.raises(EngineError, match="the version named in the period's `leader` records"):
        audit_period(run_root, 1)
