"""Branch tests for `dsl41.run_root` that the identity and retention suites do
not reach (DL-269).

Normative spec: `docs/period-model.md` ss1.1 (layout, the bundle), ss2.1
(the manifest and the segment record), ss3.2 (spellings), ss12 (the archive
receipt). Each refusal has a twin that does not trigger it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dsl41.canon import canonical_bytes
from dsl41.ir import lower_source
from dsl41.run_root import (
    Manifest,
    SourceFile,
    bundle_dir,
    bundle_source_paths,
    check_segment_version,
    genesis_manifest,
    opening_at,
    read_period_manifest,
    read_sentinel,
    require_manifest_fields,
    wal_segments,
    write_bundle,
    write_period_manifest,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_ledger import STATE_MACHINE_VERSION

_SOLO_JIL = "insert_job: j1\njob_type: c\ncommand: echo hi\nmachine: m1\n"


# ------------------------------------------------ ss3.2 spellings, one owner


# ------------------------------------------------------- the archive receipt

_RECEIPT_BODY: dict[str, Any] = {
    "estate_id": "e",
    "period_id": 2,
    "seal_digest": "sha256:" + "a" * 64,
    "attestation_digest": "sha256:" + "b" * 64,
    "chain_through_period": 3,
    "retention_class": "archive-inputs",
    "archived_at": "2026-08-21T10:00:00.000000",
    "dsl41_version": "0.1.0",
}


# --------------------------------------------------- the root's own listings


def test_ss1_1_an_unreadable_sentinel_refuses_and_an_absent_one_is_none(tmp_path: Path) -> None:
    """Absence (ENOENT) is a fact; a sentinel path that exists and cannot be
    read as a file is an EngineError naming it."""
    assert read_sentinel(tmp_path) is None
    (tmp_path / "journal.jsonl").mkdir()  # exists, and is not readable as a file
    with pytest.raises(EngineError, match=r"journal.jsonl: unreadable"):
        read_sentinel(tmp_path)


def test_ss1_1_an_unreadable_wal_directory_refuses_and_an_absent_one_is_empty(
    tmp_path: Path,
) -> None:
    assert wal_segments(tmp_path) == []
    (tmp_path / "wal").write_text("not a directory")
    with pytest.raises(EngineError, match=r"wal: unreadable"):
        wal_segments(tmp_path)


def test_ss1_1_a_dot_file_under_wal_is_a_liturgy_temporary_and_not_a_segment(
    tmp_path: Path,
) -> None:
    (tmp_path / "wal").mkdir()
    (tmp_path / "wal" / "000001.jsonl").write_text("")
    (tmp_path / "wal" / ".000002.jsonl.tmp").write_text("")
    assert wal_segments(tmp_path) == [1]


# ------------------------------------------------------------ the bundle (ss1.1)


def _bundle(tmp_path: Path) -> tuple[str, Path]:
    address = write_bundle(tmp_path, [SourceFile(path="a.jil", text="insert_job: j\n")])
    return address, bundle_dir(tmp_path, address) / "sources.json"


def _rewrite(meta: Path, mutate: Any) -> None:
    vector = json.loads(meta.read_bytes())
    mutate(vector)
    meta.write_bytes(canonical_bytes(vector))


def test_ss1_1_a_vector_with_no_sources_is_refused(tmp_path: Path) -> None:
    address, meta = _bundle(tmp_path)
    assert len(bundle_source_paths(tmp_path, address)) == 1  # the twin: one source reads
    _rewrite(meta, lambda v: v.update(sources=[]))
    with pytest.raises(EngineError, match="carries no sources"):
        bundle_source_paths(tmp_path, address)


@pytest.mark.parametrize("entry", ["a.jil", {"path": "a.jil"}, {"file": 3}])
def test_ss1_1_a_source_that_names_no_file_is_refused(tmp_path: Path, entry: object) -> None:
    address, meta = _bundle(tmp_path)
    _rewrite(meta, lambda v: v.update(sources=[entry]))
    with pytest.raises(EngineError, match="a source names no file"):
        bundle_source_paths(tmp_path, address)


def test_ss1_1_a_stored_file_that_cannot_be_read_is_refused_by_name(tmp_path: Path) -> None:
    address, _ = _bundle(tmp_path)
    [stored] = bundle_source_paths(tmp_path, address)
    stored.unlink()
    with pytest.raises(EngineError, match="unreadable bundle file"):
        bundle_source_paths(tmp_path, address)


def test_ss1_1_files_that_verify_one_by_one_must_still_reproduce_the_address(
    tmp_path: Path,
) -> None:
    """The framing hash covers the recorded PATHS too. A vector that keeps
    every file's own sha256 and changes a recorded path passes the per-file
    check and fails the address."""
    address, meta = _bundle(tmp_path)
    _rewrite(meta, lambda v: v["sources"][0].update(path="renamed.jil"))
    with pytest.raises(EngineError, match="do not reproduce the address"):
        bundle_source_paths(tmp_path, address)


# ------------------------------------------------------------ the manifest


def _manifest_file(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    manifest = genesis_manifest(
        lower_source(_SOLO_JIL), clock_domain="virtual", state_machine_version=STATE_MACHINE_VERSION
    )
    path = write_period_manifest(tmp_path, manifest)
    return path, json.loads(path.read_bytes())


def test_ss2_1_a_manifest_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    path, payload = _manifest_file(tmp_path)
    assert isinstance(read_period_manifest(tmp_path), Manifest)  # the twin: an object reads
    path.write_bytes(canonical_bytes([payload]))
    with pytest.raises(EngineError, match="not a JSON object"):
        read_period_manifest(tmp_path)


def test_ss2_1_a_runtime_profile_that_is_not_an_object_is_left_to_the_validator(
    tmp_path: Path,
) -> None:
    """`require_manifest_fields` checks the nested profile's keys only when
    it is a mapping; for anything else it says nothing, and the model's own
    validation is what refuses the file."""
    path, payload = _manifest_file(tmp_path)
    broken = {**payload, "runtime_profile": "not a profile"}
    require_manifest_fields(broken, Manifest, where="w")  # returns, raises nothing
    path.write_bytes(canonical_bytes(broken))
    with pytest.raises(EngineError, match="not a period manifest this binary can read"):
        read_period_manifest(tmp_path)


def test_ss1_1_a_bundle_sources_file_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    """`_read_canonical_file`'s own refusal, met through the bundle reader."""
    address, meta = _bundle(tmp_path)
    meta.write_bytes(canonical_bytes(["not", "an", "object"]))
    with pytest.raises(EngineError, match="not a JSON object"):
        bundle_source_paths(tmp_path, address)


# ------------------------------------------------------- the segment record


def test_ss2_1_a_segment_record_without_a_catalog_hash_version_is_refused() -> None:
    with pytest.raises(EngineError, match="missing catalog_hash_version"):
        check_segment_version({"rec": "segment"})
    check_segment_version({"rec": "segment", "catalog_hash_version": 2})  # the twin


def test_ss2_1_an_opening_record_with_no_timestamp_is_refused() -> None:
    with pytest.raises(EngineError, match="carries no timestamp"):
        opening_at({"rec": "segment"})
    assert opening_at({"rec": "segment", "at": "2026-08-19T02:00:00.000000"}).hour == 2
