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
    for name in [*generated, "incidents.conf"]:  # incidents ship with the estate
        assert (tmp_path / name).read_text() == (NB / "estate" / "bank" / name).read_text(), name


def _load_estate(estate: str, props_file: Path):
    from dsl41.ast_jil import parse
    from dsl41.ir import lower_catalog
    from dsl41.placeholders import load_properties, substitute

    bindings = load_properties([props_file])
    parsed = []
    for path in sorted((NB / "estate" / estate).glob("*.jil")):
        resolved, _ = substitute(path.read_text(), bindings, file=str(path))
        parsed.append(parse(resolved, file=str(path)))
    return lower_catalog(parsed, permit_unknown=False)


@pytest.mark.parametrize("estate", ["small", "bank"])
def test_incident_targets_exist_in_their_estate(estate: str, props_file: Path) -> None:
    """Every job an estate's incidents.conf scripts must exist in THAT
    estate's catalog (review: bank marks/trades incidents silently targeted
    small-estate names that are per-asset-class there)."""
    catalog = _load_estate(estate, props_file)
    conf = NB / "estate" / estate / "incidents.conf"
    targets = [
        line.split()[0]
        for line in conf.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert targets, "incidents.conf must script something"
    missing = [t for t in targets if t not in catalog.jobs]
    assert not missing, f"incident targets not in the {estate} estate: {missing}"


def test_runbook_job_names_exist_in_an_estate(props_file: Path) -> None:
    """The RUNBOOK is a contract: every estate job it names must exist in
    the small or bank catalog (review: two exercises referenced plays that
    could not run as written)."""
    import re

    text = (NB / "RUNBOOK.md").read_text()
    names = set(re.findall(r"\b(?:APAC|EMEA|AMER|GLOBAL|SOD|OPS)_[A-Z0-9_]*_(?:B|C|F)\b", text))
    assert len(names) > 15  # the extraction itself must keep working
    small = _load_estate("small", props_file).jobs
    bank = _load_estate("bank", props_file).jobs
    missing = sorted(n for n in names if n not in small and n not in bank)
    assert not missing, f"RUNBOOK names not in any estate: {missing}"


def test_readme_pipeline_claims_hold(props_file: Path) -> None:
    """README: viz/report/uc also accept the estate (lint + rehearse are
    pinned above). Claims are CI-substantiated, not asserted (review)."""
    report = _cli("report", *SMALL, "-p", str(props_file))
    assert report.returncode == 0, report.stdout + report.stderr
    viz = _cli("viz", "--whole-graph", *SMALL, "-p", str(props_file))
    assert viz.returncode == 0, viz.stdout + viz.stderr
    assert "flowchart" in viz.stdout  # --whole-graph emits the bare chart
    page = Path(props_file).parent / "page.html"
    html = _cli("viz", "--html", "-o", str(page), *SMALL, "-p", str(props_file))
    assert html.returncode == 0, html.stdout + html.stderr
    assert page.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert page.stat().st_size > 4_000_000  # the vendor payloads really embedded
    lens = Path(props_file).parent / "explore.html"
    explore = _cli("viz", "--explore", "-o", str(lens), *SMALL, "-p", str(props_file))
    assert explore.returncode == 0, explore.stdout + explore.stderr
    assert 'id="graph-data"' in lens.read_text(encoding="utf-8")
    assert lens.stat().st_size > 1_500_000  # the cytoscape payload really embedded
    uc = _cli("uc", *SMALL, "-p", str(props_file))
    assert uc.returncode == 0, uc.stdout + uc.stderr
    json.loads(uc.stdout)  # a bundle, not a traceback
