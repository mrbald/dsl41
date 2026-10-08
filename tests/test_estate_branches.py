"""Branch tests for src/dsl41/estate.py: every refusal of the physical roll's
read-only half and of the import, each with the passing roll beside it.

The roll's setup is test_estate.py's own: a sealed, attested period 1 in
`root_a`, then a roll into `root_b`. A refusal of the
read-only half is read through `check_roll_ready`, and the test also holds
that nothing is written. The two resumed-claim refusals need a root the roll
already claimed, so they go through `roll_into_root` and assert the refusal
only. The import's refusals go through `roll_into_root` too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import dsl41.estate as estate_mod
from dsl41.boundary import ClaimedHead, ClosedHead, EstateAnchor, default_anchor_dir, read_seal
from dsl41.estate import check_roll_ready
from dsl41.period import period_dir, seal_path, wal_path
from dsl41.runner_clock import EngineError
from dsl41.seal import Seal

from test_estate import (
    _Stopped,
    _estate,
    _invoke,
    _native_root,
    _roll,
    _seal_offline,
)


def _attested_root_a(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """`(root_a, anchor_dir, c2, root_b)`: period 1 sealed and attested."""
    c1, c2, _ = _estate(tmp_path / "estate")
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    return root_a, default_anchor_dir(root_a), c2, root_b


def _move_head(anchor_dir: Path, **update: Any) -> None:
    anchor = EstateAnchor(anchor_dir)
    anchor.acquire()
    try:
        stored = anchor.require()
        anchor.write(stored.model_copy(update={"head": stored.head.model_copy(update=update)}))
    finally:
        anchor.release()


def _interrupted_claim(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path]:
    """A roll that stopped after its import: the head is `claimed` by `root_b`."""
    root_a, anchor_dir, c2, root_b = _attested_root_a(tmp_path)
    with pytest.raises(_Stopped):
        _roll(root_b, anchor_dir, c2, stop_at="after_import")
    stored = EstateAnchor(anchor_dir).require()
    assert isinstance(stored.head, ClaimedHead)
    return root_a, anchor_dir, c2, root_b


def test_a_roll_from_a_directory_that_holds_no_lineage_refuses_and_writes_nothing(
    tmp_path: Path,
) -> None:
    root_b = tmp_path / "b"
    with pytest.raises(EngineError, match="this lineage has no anchor"):
        check_roll_ready(root_b, tmp_path / "nowhere")
    assert not root_b.exists()


def test_a_roll_refuses_a_closing_root_that_does_not_hold_the_seal_the_head_names(
    tmp_path: Path,
) -> None:
    _, anchor_dir, _, root_b = _attested_root_a(tmp_path)
    stored = EstateAnchor(anchor_dir).require()
    assert isinstance(stored.head, ClosedHead)
    _move_head(anchor_dir, seal_digest="sha256:" + "2" * 64)
    with pytest.raises(EngineError, match="the closing root does not hold the seal"):
        check_roll_ready(root_b, anchor_dir)
    assert not root_b.exists()
    # the counterpart: the head put back, the same preflight passes
    _move_head(anchor_dir, seal_digest=stored.head.seal_digest)
    assert check_roll_ready(root_b, anchor_dir).period_id == 2


def test_a_roll_refuses_a_closing_root_that_lost_the_successors_manifest(
    tmp_path: Path,
) -> None:
    root_a, anchor_dir, _, root_b = _attested_root_a(tmp_path)
    manifest = period_dir(root_a, 2) / "manifest.json"
    kept = manifest.read_bytes()
    manifest.unlink()
    with pytest.raises(EngineError, match="manifest.json is not there"):
        check_roll_ready(root_b, anchor_dir)
    assert not root_b.exists()
    manifest.write_bytes(kept)
    assert check_roll_ready(root_b, anchor_dir).period_id == 2


def test_a_resumed_claim_whose_claim_file_is_gone_refuses_by_name(tmp_path: Path) -> None:
    _, anchor_dir, c2, root_b = _interrupted_claim(tmp_path)
    anchor = EstateAnchor(anchor_dir)
    head = anchor.require().head
    assert isinstance(head, ClaimedHead)
    claim_file = anchor.claim_path(head.claim_id)
    kept = claim_file.read_bytes()
    claim_file.unlink()
    with pytest.raises(EngineError, match=r"claim .* is not there"):
        _roll(root_b, anchor_dir, c2)
    claim_file.write_bytes(kept)
    _roll(root_b, anchor_dir, c2)
    assert wal_path(root_b, 2).exists()


def test_a_resumed_claim_whose_closing_period_has_no_registry_row_refuses_by_name(
    tmp_path: Path,
) -> None:
    _, anchor_dir, c2, root_b = _interrupted_claim(tmp_path)
    anchor = EstateAnchor(anchor_dir)
    anchor.acquire()
    try:
        stored = anchor.require()
        row = stored.periods["1"]
        anchor.write(stored.model_copy(update={"periods": {}}))
    finally:
        anchor.release()
    with pytest.raises(EngineError, match="period 1 has no registry row"):
        _roll(root_b, anchor_dir, c2)
    anchor.acquire()
    try:
        stored = anchor.require()
        anchor.write(stored.model_copy(update={"periods": {"1": row}}))
    finally:
        anchor.release()
    _roll(root_b, anchor_dir, c2)
    assert wal_path(root_b, 2).exists()


def test_the_import_copies_files_of_the_opening_period_and_skips_directories(
    tmp_path: Path,
) -> None:
    root_a, anchor_dir, c2, root_b = _attested_root_a(tmp_path)
    stray = period_dir(root_a, 2) / "scratch"
    stray.mkdir()
    (stray / "inner.txt").write_text("not part of the boundary")
    _roll(root_b, anchor_dir, c2)
    assert (period_dir(root_b, 2) / "manifest.json").is_file()
    assert not (period_dir(root_b, 2) / "scratch").exists()


def test_the_import_refuses_a_manifest_that_did_not_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write of the manifest is lost; the import reads back nothing."""
    root_a, anchor_dir, c2, root_b = _attested_root_a(tmp_path)
    real = estate_mod.durable_write

    def losing(path: str, data: bytes) -> None:
        if path.endswith("manifest.json") and "periods" in path:
            return
        real(path, data)

    monkeypatch.setattr(estate_mod, "durable_write", losing)
    with pytest.raises(EngineError, match="the imported manifest is not the one the boundary"):
        _roll(root_b, anchor_dir, c2)
    assert not wal_path(root_b, 2).exists()
    # the counterpart: with the write restored, the same roll goes through
    monkeypatch.setattr(estate_mod, "durable_write", real)
    _roll(root_b, anchor_dir, c2)
    assert wal_path(root_b, 2).exists()


def test_the_import_refuses_a_manifest_that_landed_as_another_valid_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest read back is present and valid but is not the boundary's."""
    root_a, anchor_dir, c2, root_b = _attested_root_a(tmp_path)
    real = estate_mod.durable_write
    first = period_dir(root_a, 1) / "manifest.json"
    assert first.is_file()

    def swapping(path: str, data: bytes) -> None:
        if path.endswith("manifest.json") and "000002" in path:
            data = first.read_bytes()
        real(path, data)

    monkeypatch.setattr(estate_mod, "durable_write", swapping)
    with pytest.raises(EngineError, match="the imported manifest is not the one the boundary"):
        _roll(root_b, anchor_dir, c2)
    assert not wal_path(root_b, 2).exists()


def test_the_import_refuses_a_sidecar_that_landed_as_another_valid_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copied sidecar parses, but it digests to something other than the seal rolled from."""
    root_a, anchor_dir, c2, root_b = _attested_root_a(tmp_path)
    seal = read_seal(root_a, 1)
    payload = seal.to_payload()
    payload["state"]["jobs"]["a"] = {
        **payload["state"]["jobs"]["a"],
        "status": "RUNNING",
        "run_number": 1,
    }
    payload["executions"] = [
        {
            "kind": "bound",
            "job": "a",
            "run_number": 1,
            "effect_id": "e-1",
            "index": 1,
            "run_id": "11111111-1111-4111-8111-111111111111",
            "executor_id": "local",
            "generation": 0,
            "run_dir": "runs/a.1",
        }
    ]
    other = Seal(**payload)
    assert other.digest != seal.digest
    real = estate_mod.durable_write
    target_sidecar = str(seal_path(root_b, 1))

    def swapping(path: str, data: bytes) -> None:
        real(path, other.to_bytes() if path == target_sidecar else data)

    monkeypatch.setattr(estate_mod, "durable_write", swapping)
    with pytest.raises(EngineError, match="the imported sidecar digests to something other"):
        _roll(root_b, anchor_dir, c2)
    assert not wal_path(root_b, 2).exists()
    assert json.loads(Path(target_sidecar).read_bytes())["digest"] == other.digest
