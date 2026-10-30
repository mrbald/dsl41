"""run_root.py's refusals, one test per branch the other suites do not reach (DL-269)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from dsl41.ir import lower_source
from dsl41.run_root import (
    SourceFile,
    bundle_dir,
    check_root_unused,
    check_segment_record,
    claim_root,
    genesis_manifest,
    load_bundle_catalog,
    read_sentinel,
    segment_record,
    sentinel_path,
    wal_segments,
    write_bundle,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_ledger import STATE_MACHINE_VERSION

T0 = datetime(2026, 7, 1, 8, 0)


def _segment() -> dict[str, object]:
    catalog = lower_source("insert_job: a\njob_type: c\ncommand: x\n")
    manifest = genesis_manifest(
        catalog, clock_domain="virtual", state_machine_version=STATE_MACHINE_VERSION
    )
    return segment_record(manifest, estate_id="e1", at=T0)


def test_a_journal_line_that_is_not_json_is_no_sentinel(tmp_path: Path) -> None:
    sentinel_path(tmp_path).write_text("not json\n")
    assert read_sentinel(tmp_path) is None


def test_a_period_root_line_this_binary_cannot_read_refuses_by_name(tmp_path: Path) -> None:
    sentinel_path(tmp_path).write_text('{"rec": "period_root", "estate_id": "e", "x": 1.5}\n')
    with pytest.raises(EngineError, match="not a sentinel this binary can read"):
        read_sentinel(tmp_path)


def test_a_foreign_file_under_wal_refuses(tmp_path: Path) -> None:
    (tmp_path / "wal").mkdir()
    (tmp_path / "wal" / "notes.txt").write_text("x")
    with pytest.raises(EngineError, match="not a segment file"):
        wal_segments(tmp_path)


def test_a_journal_that_is_not_ours_refuses_the_claim(tmp_path: Path) -> None:
    sentinel_path(tmp_path).write_text("not json\n")
    with pytest.raises(EngineError, match="already exists and is not ours"):
        claim_root(tmp_path)


def test_a_bundle_with_no_sources_json_refuses(tmp_path: Path) -> None:
    address = write_bundle(tmp_path, [SourceFile(path="a.jil", text="insert_job: a\n")])
    (bundle_dir(tmp_path, address) / "sources.json").unlink()
    with pytest.raises(EngineError, match="no readable sources.json"):
        load_bundle_catalog(tmp_path, address)


def test_a_bundle_that_does_not_lower_refuses(tmp_path: Path) -> None:
    address = write_bundle(tmp_path, [SourceFile(path="a.jil", text="job_type: c\n")])
    with pytest.raises(EngineError, match="does not load"):
        load_bundle_catalog(tmp_path, address)


def test_a_segment_record_missing_a_key_refuses() -> None:
    record = _segment()
    del record["baseline_id"]
    with pytest.raises(EngineError, match="missing baseline_id"):
        check_segment_record(record)


def test_a_segment_whose_number_is_not_its_period_refuses() -> None:
    record = {**_segment(), "segment_no": 2}
    with pytest.raises(EngineError, match="declares period 1"):
        check_segment_record(record)


def test_a_later_segment_must_open_its_predecessors_seal() -> None:
    link = {"period_id": 5, "digest": "sha256:" + "0" * 64}
    record = {**_segment(), "segment_no": 2, "period_id": 2, "opens_from_seal": link}
    with pytest.raises(EngineError, match="opens_from_seal names period"):
        check_segment_record(record)


def test_a_root_with_committed_periods_or_populated_runs_is_not_unused(tmp_path: Path) -> None:
    (tmp_path / "periods" / "000001").mkdir(parents=True)
    (tmp_path / "runs" / "a.1").mkdir(parents=True)
    with pytest.raises(EngineError, match=r"periods \(committed\), runs \(populated\)"):
        check_root_unused(tmp_path)


def test_a_later_segment_that_opens_its_predecessors_seal_passes_the_record_check() -> None:
    """The record schema still reads a 1.x later segment; `require_period_one` is what refuses
    such a root."""
    link = {"period_id": 1, "digest": "sha256:" + "0" * 64}
    check_segment_record({**_segment(), "segment_no": 2, "period_id": 2, "opens_from_seal": link})
