"""Smoke tests for examples/nightbank: the estate loads, lints clean, and a
full virtual-clock night (with the operator's SET_GLOBAL/OFF_HOLD actions
scripted as events) reaches the SOD flip. The live-engine path is exercised
manually via the RUNBOOK; these tests pin what CI can pin."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NB = ROOT / "examples" / "nightbank"
SMALL = sorted(str(p) for p in (NB / "estate" / "small").glob("*.jil"))
BANK = sorted(str(p) for p in (NB / "estate" / "bank").glob("*.jil"))


def _launcher():
    loader = SourceFileLoader("nightbank_launcher", str(NB / "bin" / "nightbank"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dsl41", *args], capture_output=True, text=True, cwd=ROOT
    )


@pytest.fixture(scope="module")
def props_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    run_dir = tmp_path_factory.mktemp("nightbank")
    launcher = _launcher()
    props = launcher.compute_properties(run_dir, datetime(2026, 1, 6, 0, 5, tzinfo=UTC), 3)
    path = run_dir / "night.properties"
    launcher.write_kv(path, props)
    return path


def test_props_convert_anchors_to_region_local_time(props_file: Path) -> None:
    props = dict(line.split("=", 1) for line in props_file.read_text().splitlines())
    assert props["EOD_APAC"] == "09:05"  # 00:05 UTC in Asia/Tokyo
    assert props["EOD_EMEA"] == "01:08"  # +3 min stagger, Europe/Zurich (CET)
    assert props["EOD_AMER"] == "19:11"  # +6 min, America/New_York (prev. evening)


@pytest.mark.parametrize("files", [SMALL, BANK], ids=["small", "bank"])
def test_estate_lints_clean(files: list[str], props_file: Path) -> None:
    result = _cli("lint", *files, "-p", str(props_file))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == ""  # zero findings, not merely zero errors


def test_small_estate_full_night_rehearsal(props_file: Path, tmp_path: Path) -> None:
    scenario = {
        "adapter": {"default": [20, 0]},
        "events": [
            {
                "at": "2026-01-06T00:40:00",
                "kind": "SET_GLOBAL",
                "payload": {"name": f"RECON_{region}", "value": "CLEAN"},
            }
            for region in ("APAC", "EMEA", "AMER")
        ]
        + [
            {"at": "2026-01-06T00:43:00", "kind": "OFF_HOLD", "payload": {"job": "SOD_APPROVE_C"}},
        ],
    }
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    result = _cli(
        "rehearse",
        *SMALL,
        "-p",
        str(props_file),
        "--scenario",
        str(scenario_path),
        "--start",
        "2026-01-06T00:00:00",
        "--hours",
        "2",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    trace = result.stdout

    for line in (
        "APAC_EOD_B RUNNING->SUCCESS",
        "EMEA_EOD_B RUNNING->SUCCESS",
        "AMER_EOD_B RUNNING->SUCCESS",
        "GLOBAL_RISK_B RUNNING->SUCCESS",
        "SOD_APPROVE_C ON_HOLD",
        "SOD_FLIP_C RUNNING->SUCCESS",
        "SOD_WARMUP_C RUNNING->SUCCESS",
        "SOD_B RUNNING->SUCCESS",
        "OPS_ARCHIVE_C INACTIVE->QUE_WAIT",  # tape-drive mutex
    ):
        assert line in trace, f"missing: {line}"

    # Grid contention: two EMEA shards queue; priority admits shard 4 first.
    assert "EMEA_VAL_SHARD3_C INACTIVE->QUE_WAIT" in trace
    assert "EMEA_VAL_SHARD4_C INACTIVE->QUE_WAIT" in trace
    assert trace.index("EMEA_VAL_SHARD4_C QUE_WAIT->STARTING") < trace.index(
        "EMEA_VAL_SHARD3_C QUE_WAIT->STARTING"
    )


def test_bank_estate_regenerates_byte_stable(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(NB / "generate.py"), "--out", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    generated = sorted(p.name for p in tmp_path.glob("*.jil"))
    assert generated == sorted(Path(p).name for p in BANK)
    for name in generated:
        assert (tmp_path / name).read_text() == (NB / "estate" / "bank" / name).read_text(), name
