"""examples/nightbank: the estate loads, lints clean, a full virtual-clock
night reaches the SOD flip -- and, since S7b, survives its faults.

The last sentence here used to read "the live-engine path is exercised
manually via the RUNBOOK; these tests pin what CI can pin", and
docs/concurrency-model.md quotes it as the gap the whole phase-12 programme
closes. The virtual-clock half of it is closed: the sweep at the foot of
this file drives the real 81-job night through seeded interleavings of
leader failover, a spawn decided and never acted on, duplicated and stale
completions, quarantine and drain, and checks CM-14 and CM-09 over every
one. What is still manual is the REAL-PROCESS path -- actual subprocesses,
actual signals, actual wall clock -- which ss9 calls a separate tier and
DL-107 scopes as S7c."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from dsl41.oracle_state import Event

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


def load_estate(estate: str, props_file: Path):
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
    catalog = load_estate(estate, props_file)
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
    small = load_estate("small", props_file).jobs
    bank = load_estate("bank", props_file).jobs
    missing = sorted(n for n in names if n not in small and n not in bank)
    assert not missing, f"RUNBOOK names not in any estate: {missing}"


def test_readme_pipeline_claims_hold(props_file: Path) -> None:
    """README: viz/report/uc also accept the estate (lint + rehearse are
    pinned above). Claims are CI-substantiated, not asserted (review)."""
    report = _cli("report", *SMALL, "-p", str(props_file))
    assert report.returncode == 0, report.stdout + report.stderr
    viz = _cli("viz", "--format", "chart", *SMALL, "-p", str(props_file))
    assert viz.returncode == 0, viz.stdout + viz.stderr
    assert "flowchart" in viz.stdout  # --format chart emits the bare chart
    page = Path(props_file).parent / "page.html"
    html = _cli("viz", "--format", "html", "-o", str(page), *SMALL, "-p", str(props_file))
    assert html.returncode == 0, html.stdout + html.stderr
    assert page.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert page.stat().st_size > 4_000_000  # the vendor payloads really embedded
    lens = Path(props_file).parent / "explore.html"
    explore = _cli("viz", "--format", "explore", "-o", str(lens), *SMALL, "-p", str(props_file))
    assert explore.returncode == 0, explore.stdout + explore.stderr
    assert 'id="graph-data"' in lens.read_text(encoding="utf-8")
    assert lens.stat().st_size > 1_500_000  # the cytoscape payload really embedded
    uc = _cli("uc", *SMALL, "-p", str(props_file))
    assert uc.returncode == 0, uc.stdout + uc.stderr
    json.loads(uc.stdout)  # a bundle, not a traceback


# ------------------- the concurrency matrix over the proving ground (S7b)
#
# docs/concurrency-model.md ss9 names this estate as the proving ground and
# says why: "Every property here is a property of that estate under injected
# faults, not an assertion in prose." Until S7b the properties were held over
# a four-job fixture, and this file's own docstring recorded the gap -- "the
# live-engine path is exercised manually via the RUNBOOK; these tests pin
# what CI can pin". A seeded sweep over the real night is what closes the
# virtual-clock half of it.

NIGHT_START = datetime(2026, 1, 6, 0, 0)
NIGHT_SEEDS = range(16)
NIGHT_STEPS = 8


@pytest.fixture(scope="module")
def small_catalog(props_file: Path):
    """Built once: 81 jobs across five files with placeholder substitution
    is 40ms, and a sweep that paid it per seed would spend its time on
    parsing rather than on interleavings."""
    return load_estate("small", props_file)


async def _night(catalog, run_root: Path, schedule):
    """One night, driven through NIGHT_STEPS boundaries with the schedule's
    faults fired at each.

    The operator actions are the RUNBOOK's own: three regional recon flags
    and the hold released on SOD_APPROVE_C. Without them the night stalls
    before the SOD flip, and a sweep that never reached the flip would be
    testing the first half of an estate."""
    from model_harness import ModelRun
    from dsl41.runner_scheduler import Scheduler

    run = ModelRun(
        catalog,
        run_root,
        default=(20.0, 0),
        start=NIGHT_START,
        scheduler=lambda at: Scheduler(catalog, start=at, default_tz="UTC"),
    )
    run.start()
    for region in ("APAC", "EMEA", "AMER"):
        run.inject(
            Event(
                at=NIGHT_START + timedelta(minutes=40),
                kind="SET_GLOBAL",
                payload={"name": f"RECON_{region}", "value": "CLEAN"},
            )
        )
    run.inject(
        Event(
            at=NIGHT_START + timedelta(minutes=43),
            kind="OFF_HOLD",
            payload={"job": "SOD_APPROVE_C"},
        )
    )
    for step in range(NIGHT_STEPS):
        await run.run_to(NIGHT_START + timedelta(minutes=15 * step))
        await schedule.at(run, step)
    await run.settle(NIGHT_START + timedelta(hours=3))
    return run


@pytest.mark.parametrize("seed", NIGHT_SEEDS)
def test_cm14_the_night_survives_its_faults(small_catalog, tmp_path: Path, seed: int) -> None:
    """CM-14 and CM-09 over the estate ss9 names, under the faults one host
    can suffer: leader failover mid-night, a spawn decided and never acted
    on, duplicated and stale completions, quarantine, and a drain under
    in-flight work.

    A real estate is a different test from a four-job fixture, and not only
    in size: nightbank has boxes whose members cascade, a resource mutex
    that makes jobs queue, cross-region conditions, and a scheduler firing
    start_times -- so a double run shows up as a second CASCADE, and a
    resume that mishandled the scheduler shows up as a job that ran twice a
    quarter of an hour apart."""
    from model_harness import FaultSchedule

    schedule = FaultSchedule.build(seed, steps=NIGHT_STEPS)

    async def scenario():
        return await _night(small_catalog, tmp_path / "run", schedule)

    run = asyncio.run(scenario())
    try:
        run.check()
    except AssertionError as violation:  # pragma: no cover -- only on a real bug
        raise AssertionError(f"{violation}\n  schedule: {schedule.describe()}") from violation
    assert len(run.log.execs()) > 10, "a night that dispatched nothing proves nothing"


def test_the_night_completes_end_to_end_in_the_harness(small_catalog, tmp_path: Path) -> None:
    """The sweep's baseline, without which `execs > 10` is a weak guard: a
    driver that stalled the night at its first box would still dispatch more
    than ten jobs and still pass every seed.

    So: no faults, and the night must reach the SOD flip -- the same
    end-state the rehearsal test asserts through the CLI, reached here
    through the engine the sweep perturbs."""
    from model_harness import FaultSchedule

    quiet = FaultSchedule(seed=-1, plan={})

    async def scenario():
        return await _night(small_catalog, tmp_path / "run", quiet)

    run = asyncio.run(scenario())
    run.check()
    assert quiet.fired == []
    store = run.live.oracle.store.job
    for job in ("APAC_EOD_B", "EMEA_EOD_B", "AMER_EOD_B", "GLOBAL_RISK_B", "SOD_FLIP_C", "SOD_B"):
        assert store[job].status == "SUCCESS", f"{job} is {store[job].status}"
    assert len(run.log.execs()) > 50  # a whole night, not a corner of one
