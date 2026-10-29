"""DL-412, DL-420: the release's service-side reads of the run root go through the package's
own readers, and unreadable evidence is told apart from absent evidence; the release's
records are written by the durability liturgy, a short write included. The run roots are
tests/release_root.py's, as the stub engine writes them."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from dsl41 import release, release_service
from dsl41.period import period_dir, wal_path
from dsl41.release_service import EXIT_STOPPED, Config, Stop
from dsl41.runner_procid import durable_write
from release_root import genesis, open_next, seal

RID = "0" * 8 + "-0000-4000-8000-" + "0" * 12


def _config(base: Path) -> Config:
    root = base / "run"
    root.mkdir(parents=True)
    return Config(
        launcher=base / "dsl41-launch",
        dsl41="dsl41",
        run_root=root,
        estate_anchor=base / "anchor",
        releases=base / "releases",
        active=base / "active",
        environment="sandbox",
        properties=str(base / "site.properties"),
        max_jobs=10,
        max_grace_s=60.0,
        next_profile=[],
        service_account="svc",
        engine_unit="dsl41-engine.service",
    )


def _sealed(base: Path, *, opened: bool = True) -> Config:
    """A root whose period 1 is sealed under RID into the release `v2`, and opened."""
    cfg = _config(base)
    genesis(cfg.run_root)
    jil = cfg.releases / "v2" / "a.jil"
    jil.parent.mkdir(parents=True)
    jil.write_text("insert_job: A\njob_type: c\ncommand: x\nmachine: m\n")
    seal(
        cfg.run_root,
        1,
        request_id=RID,
        actor="os/svc",
        epoch=1,
        closes_at_index=0,
        files=[str(jil)],
    )
    if opened:
        open_next(cfg.run_root, 1, 2)
    return cfg


def _corrupt(path: Path) -> None:
    """An interior line that is not JSON: corruption, never a torn tail."""
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], "{not json", *lines[1:]]) + "\n")


def test_a_committed_seal_is_read_by_the_lineage_proof(tmp_path: Path) -> None:
    cfg = _sealed(tmp_path)
    found = release_service.committed(cfg, RID)
    assert found["committed"] is True and (found["period"], found["next_period"]) == (1, 2)
    assert found["actor"] == "os/svc"
    assert release_service.committed(cfg, RID.replace("0", "1", 1)) == {"committed": False}


def test_an_unreadable_lineage_stops_and_is_not_read_as_no_seal(tmp_path: Path) -> None:
    cfg = _sealed(tmp_path)
    _corrupt(wal_path(cfg.run_root, 2))
    with pytest.raises(Stop) as stopped:
        release_service.committed(cfg, RID)
    assert stopped.value.code == EXIT_STOPPED
    assert "cannot be read" in stopped.value.lines[0]


def test_the_pins_of_an_opened_period_prove_its_opening(tmp_path: Path) -> None:
    cfg = _sealed(tmp_path)
    pins = release_service.service_pins(cfg, RID)
    assert pins["opened"] is True and pins["opening_refusal"] is None
    assert pins["leader_epoch"] == 2 and pins["unreadable"] == []
    assert pins["manifest_source_bundle_hash"] == pins["source_bundle_hash"]
    assert pins["sources"] == [str(cfg.releases / "v2" / "a.jil")]
    with pytest.raises(Stop, match="no committed seal"):
        release_service.service_pins(cfg, RID.replace("0", "1", 1))


def test_the_pins_tell_an_absent_segment_from_an_unreadable_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A period not opened yet has no segment: absent. A segment that cannot be read stops
    the pins, exit 3, naming it; the lineage is held fixed so this read is the one tested."""
    cfg = _sealed(tmp_path, opened=False)
    pins = release_service.service_pins(cfg, RID)
    assert pins["opened"] is False and pins["unreadable"] == []
    found = release_service.committed(cfg, RID)
    open_next(cfg.run_root, 1, 2)
    _corrupt(wal_path(cfg.run_root, 2))
    monkeypatch.setattr(release_service, "committed", lambda *_: found)
    with pytest.raises(Stop) as stopped:
        release_service.service_pins(cfg, RID)
    assert stopped.value.code == EXIT_STOPPED
    assert stopped.value.lines[0].startswith(f"{wal_path(cfg.run_root, 2)} cannot be read")


def test_an_opening_the_proof_refuses_is_named(tmp_path: Path) -> None:
    """The lineage proof reads only the link's digest; the opening proof also holds the
    segment to the seal's instant and its next period, as the opener wrote them."""
    cfg = _sealed(tmp_path)
    segment = wal_path(cfg.run_root, 2)
    lines = segment.read_text().splitlines()
    opening = json.loads(lines[0]) | {"at": "2000-01-01T00:00:00"}
    segment.write_text("\n".join([json.dumps(opening), *lines[1:]]) + "\n")
    pins = release_service.service_pins(cfg, RID)
    assert pins["opened"] is True and "an unproved opening" in pins["opening_refusal"]


def test_an_unreadable_manifest_or_bundle_is_named_apart(tmp_path: Path) -> None:
    cfg = _sealed(tmp_path)
    manifest = period_dir(cfg.run_root, 2) / "manifest.json"
    text = manifest.read_text()
    bundle = json.loads(text)["source_bundle_hash"].split(":")[-1]
    sources = cfg.run_root / "catalogs" / bundle / "sources.json"
    sources.write_text("{}")
    pins = release_service.service_pins(cfg, RID)
    assert pins["sources"] == [] and pins["manifest_source_bundle_hash"] is not None
    assert [line.split(":")[0] for line in pins["unreadable"]] == [
        "period 2's bundle cannot be read"
    ]
    manifest.write_text("[]")
    pins = release_service.service_pins(cfg, RID)
    assert pins["manifest_source_bundle_hash"] is None
    assert pins["unreadable"][0].startswith("period 2's manifest cannot be read")
    manifest.unlink()
    pins = release_service.service_pins(cfg, RID)
    assert pins["manifest_source_bundle_hash"] is None and pins["unreadable"] == []


def test_hold_history_reads_the_log_and_refuses_an_unreadable_one(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    genesis(cfg.run_root)
    segment = wal_path(cfg.run_root, 1)
    records: list[dict[str, Any]] = [
        {"rec": "input", "seq": 1, "kind": "ON_HOLD", "request_id": "a", "payload": {"job": "J"}},
        {"rec": "decision", "index": 1, "decision": "applied", "revisions": {"job:J": 4}},
        {"rec": "input", "seq": 2, "kind": "OFF_HOLD", "request_id": "b", "payload": {"job": "J"}},
        {"rec": "decision", "index": 2, "decision": "rejected", "revisions": {}},
        {"rec": "input", "seq": 3, "kind": "STARTJOB", "request_id": "c", "payload": {"job": "J"}},
        {"rec": "input", "seq": 4, "kind": "ON_HOLD", "request_id": "d", "payload": {"job": "J"}},
    ]
    with segment.open("a") as stream:
        for record in records:
            if record["rec"] == "decision":
                record["legacy_batch"] = False
            stream.write(json.dumps(record) + "\n")
    assert release_service.hold_history(cfg, "J") == {
        "changes": [
            {"request_id": "a", "kind": "ON_HOLD", "decision": "applied", "rev": 4},
            {"request_id": "d", "kind": "ON_HOLD", "decision": "undecided", "rev": None},
        ]
    }
    _corrupt(segment)
    with pytest.raises(Stop, match="cannot be read") as stopped:
        release_service.hold_history(cfg, "J")
    assert stopped.value.code == EXIT_STOPPED


@pytest.mark.parametrize("rec", ["input", "decision"])
def test_hold_history_refuses_a_record_without_its_attempt_number(tmp_path: Path, rec: str) -> None:
    """DL-420: an input without `seq`, or a decision without `index`, would join on nothing;
    it is unreadable evidence, a stop with exit 3 that names the line."""
    cfg = _config(tmp_path)
    genesis(cfg.run_root)
    record: dict[str, Any] = {"rec": rec, "kind": "ON_HOLD", "payload": {"job": "J"}}
    if rec == "decision":
        record = {"rec": rec, "seq": True, "decision": "rejected", "legacy_batch": False}
    with wal_path(cfg.run_root, 1).open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    with pytest.raises(Stop) as stopped:
        release_service.hold_history(cfg, "J")
    assert stopped.value.code == EXIT_STOPPED
    assert f"line 2: an {rec} record without its attempt number" in stopped.value.lines[0]


def test_an_engine_answer_that_is_not_json_refuses_and_is_never_no_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-420: `dsl41 query` that exits 0 with no JSON is a refusal, exit 2, as before; the
    successor wait stops on it rather than reading it as "no answer yet", and status reports
    it."""
    import subprocess

    cfg = _config(tmp_path)
    garbled = subprocess.CompletedProcess(args=[], returncode=0, stdout="garbage", stderr="")
    monkeypatch.setattr(release.subprocess, "run", lambda *_, **__: garbled)
    sealed = {"next_baseline": "b", "epoch": 1}
    apply = release.Apply(cfg, {"candidate": "v2", "sealed": sealed}, release.Waits())
    with pytest.raises(Stop) as refused:
        apply.successor_answers()
    assert not isinstance(refused.value, release.NoAnswer)
    assert refused.value.code == release_service.EXIT_REFUSED
    assert refused.value.lines == ("`dsl41 query status` printed no JSON.",)
    assert release.engine_answer(cfg) == {"answers": False, "error": "it printed no JSON"}


def _verified(tmp_path: Path, pins: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The problems `Apply.verify` records from PINS."""
    cfg = _config(tmp_path)
    cfg.state.mkdir()
    monkeypatch.setattr(release, "service_json", lambda *_: pins)
    apply = release.Apply(
        cfg,
        {"candidate": "v2", "previous": "v1", "request_id": RID, "step": "opened"},
        release.Waits(),
    )
    with pytest.raises(Stop):
        apply.verify()
    problems: list[str] = apply.record["problems"]
    return problems


def test_verify_names_a_missing_manifest_and_unreadable_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = {
        "source_bundle_hash": "sha256:" + "1" * 64,
        "next_period": 2,
        "epoch": 1,
        "opened": True,
        "opening_refusal": None,
        "leader_epoch": 2,
        "manifest_source_bundle_hash": None,
        "sources": [],
        "unreadable": ["period 2's bundle cannot be read: gone"],
    }
    assert _verified(tmp_path, pins, monkeypatch) == [
        "period 2 has no manifest",
        "period 2's bundle cannot be read: gone",
    ]


def test_a_short_write_still_lands_the_whole_release_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-151: `os.write` may write fewer bytes than given; the record is written whole, so a
    resume never reads a truncated apply.json. The release's own writes, apply.json and the
    receipts in .release, both go through the one liturgy."""
    real = os.write
    calls: list[int] = []

    def short(fd: int, data: bytes) -> int:
        calls.append(len(data))
        return real(fd, bytes(data[:7]))

    record = {"candidate": "v2", "holds": [{"job": f"J{n}", "state": "applied"} for n in range(9)]}
    path = tmp_path / "apply.json"
    receipts = tmp_path / ".release"
    receipts.mkdir()
    fd = os.open(receipts, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "write", short)
            release_service.write_json(path, record)
            # a receipt, as `write_in_release` writes it through .release's descriptor
            durable_write("applied-x.json", json.dumps(record).encode(), mode=0o644, dir_fd=fd)
    finally:
        os.close(fd)
    assert len(calls) > 2 and json.loads(path.read_text()) == record
    assert json.loads((receipts / "applied-x.json").read_text()) == record
    assert sorted(entry.name for entry in receipts.iterdir()) == ["applied-x.json"]
    for written in (path, receipts / "applied-x.json"):
        assert oct(written.stat().st_mode & 0o777) == "0o644"
