"""Definition hash compatibility and bounded reload differences (DL-482)."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from pathlib import Path
import hashlib

import pytest

from dsl41 import reread_worker, reload_plan
from dsl41.ir import lower_source
from dsl41.reload_diff import definition_hash, definition_record, diff, record_difference
from dsl41.reload_plan import Live, RereadRefused, check_reload_candidate
from dsl41.runner_adapters import LINE_LIMIT
from dsl41.runner_preflight import PreflightItem

HASH_JIL = """insert_machine: m1
type: a
node_name: host

calendar: hols
01/01/2026 00:00

insert_job: j
job_type: c
command: echo hello
machine: m1
condition: s(other)
date_conditions: 1
run_calendar: hols
start_times: "01:00"
"""
JOB = "insert_job: j\njob_type: c\ncommand: true\nmachine: localhost\n"


def difference_estate(tmp_path, catalog, settings):
    from test_main_config import estate_file
    from dsl41 import main_config

    candidate = main_config.prepare(estate_file(tmp_path))
    return replace(candidate, catalog=catalog, settings={**candidate.settings, **settings})


@pytest.mark.parametrize(
    "settings,expected",
    [
        ({}, "4c669a16cdc2d8f65993782242933e4e43b76c18eaf500d21162372738d59181"),
        ({"timezone": "UTC"}, "e9da9dfd2b40866d528ce74f2d8056461c24f27c4dac67c5b2936d47482c3111"),
        (
            {
                "timezone": "UTC",
                "job_env": {"K": "V"},
                "kill_signals": [15, 9],
                "fw_default_interval_s": 60,
            },
            "e9da9dfd2b40866d528ce74f2d8056461c24f27c4dac67c5b2936d47482c3111",
        ),
    ],
)
def test_hashes_match_pre_blk2_bytes(settings, expected):
    catalog = lower_source(HASH_JIL)
    assert definition_hash(catalog.jobs["j"], catalog, settings) == "sha256:" + expected


def real_estates():
    """Use complete Nightbank estates and all lowerable corpus fixtures (DL-482)."""
    from dsl41.ast_jil import parse
    from dsl41.ir import LoweringError, lower_catalog
    from dsl41.placeholders import substitute

    root = Path(__file__).resolve().parents[1]
    for path in sorted((root / "tests/corpus").glob("*.jil")):
        if path.name == "sem31_xor.jil":
            with pytest.raises(LoweringError):
                lower_source(path.read_text())
            continue
        yield path.relative_to(root).as_posix(), lower_source(path.read_text())
    bindings = {
        "OWNER": "synthetic",
        "NB_DATA": "/synthetic/data",
        "NB_LOGS": "/synthetic/logs",
        "NB_PROFILE": "/synthetic/profile",
        "HB_WINDOW": "00:00-23:59",
        "EOD_AMER": "01:00",
        "EOD_APAC": "01:00",
        "EOD_EMEA": "01:00",
    }
    for size in ("small", "bank"):
        paths = sorted((root / "examples/nightbank/estate" / size).glob("*.jil"))
        parsed = [
            parse(substitute(path.read_text(), bindings, file=path.name)[0], file=path.name)
            for path in paths
        ]
        yield "nightbank/" + size, lower_catalog(parsed, permit_unknown=False)


# DL-482: the main 81cc690 manifest covers every job, including changed definitions.
MAIN_ESTATE_HASH_MANIFEST = "1f3c975ce85fb49c44bcb272d15d5041a462033307c70f4f7eb910f1fda82c1d"


def estate_hash_manifest(hash_function=definition_hash):
    manifest = []
    for name, catalog in real_estates():
        modified = catalog.model_copy(deep=True)
        for job in modified.jobs.values():
            job.passthrough["description"] = "synthetic definition delta"
        for changed, candidate in enumerate((catalog, modified)):
            for settings in (
                {},
                {"timezone": "UTC"},
                {
                    "timezone": "UTC",
                    "job_env": {"K": "V"},
                    "kill_signals": [15, 9],
                    "fw_default_interval_s": 60,
                },
            ):
                plan = diff(catalog, candidate, settings, settings)
                changes = {change.name for change in plan.changed}
                for job_name in sorted(catalog.jobs):
                    before = definition_record(catalog.jobs[job_name], catalog, settings)
                    after = definition_record(candidate.jobs[job_name], candidate, settings)
                    assert (job_name in changes) == bool(record_difference(before, after)), (
                        name,
                        job_name,
                    )
                    manifest.append(
                        [
                            name,
                            changed,
                            job_name,
                            settings,
                            hash_function(candidate.jobs[job_name], candidate, settings),
                        ]
                    )
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def test_real_estates_difference_invariant_and_every_main_hash():
    assert estate_hash_manifest() == MAIN_ESTATE_HASH_MANIFEST


def test_missing_null_and_boolean_integer_are_different():
    assert record_difference({}, {"x": None})
    assert record_difference({"x": True}, {"x": 1})
    assert record_difference({"x": [True]}, {"x": [1]})
    assert not record_difference({"b": 1, "a": 2}, {"a": 2, "b": 1})


@pytest.mark.parametrize("size", [240, 5000])
def test_shared_reference_and_setting_are_sent_once_with_bounded_values(
    monkeypatch, tmp_path, size
):
    old = lower_source(JOB + "insert_machine: localhost\ntype: a\nnode_name: " + "a" * 6000 + "\n")
    new = lower_source(JOB + "insert_machine: localhost\ntype: a\nnode_name: " + "b" * 6000 + "\n")
    old_job, new_job = old.jobs["j"], new.jobs["j"]
    old.jobs.clear()
    new.jobs.clear()
    for index in range(size):
        name = f"job_{index:04}"
        old.jobs[name] = old_job.model_copy(update={"name": name})
        new.jobs[name] = new_job.model_copy(update={"name": name})
    monkeypatch.setattr(reread_worker, "preflight_items", lambda *a: [])
    monkeypatch.setattr(reload_plan, "preflight_items", lambda *a: [])
    candidate = difference_estate(tmp_path, new, {"job_env": {"V": "y" * 6000}})
    live = Live(
        old,
        {**candidate.settings, "job_env": {"V": "x" * 6000}},
        1,
        {},
        launch=candidate.metadata["launch"],
    )
    view = reread_worker.preview(candidate, live)
    groups = view["definition_changes"]["groups"]
    assert len(groups) == 2
    assert sorted(len(group["jobs"]) for group in groups) == [0, size]
    assert all(len(group["before"]) <= 120 and len(group["after"]) <= 120 for group in groups)
    assert any("[cut]" in group["before"] for group in groups)
    assert "x" * 120 not in json.dumps(groups) and "y" * 120 not in json.dumps(groups)
    assert len(json.dumps({"ok": True, "preview": view}).encode()) < LINE_LIMIT
    monkeypatch.setattr(
        reread_worker, "definition_changes", lambda *a: pytest.fail("update built differences")
    )
    update = reread_worker.preview(candidate, live, include_definition_changes=False)
    assert "definition_changes" not in update
    assert "lint_summary" not in update
    assert len(json.dumps({"ok": True, "preview": update}).encode()) < LINE_LIMIT


def test_update_worker_skips_difference_builder(monkeypatch, tmp_path):
    catalog = lower_source(JOB)
    live = Live(catalog, {}, 1, {})
    candidate = difference_estate(tmp_path, catalog, {})
    from dsl41 import runner_update

    monkeypatch.setattr(reread_worker, "prepare", lambda _: candidate)
    monkeypatch.setattr(
        reread_worker, "preview", lambda *a, **kw: {"included": kw["include_definition_changes"]}
    )
    monkeypatch.setattr(reread_worker, "reread", lambda *a, **kw: SimpleNamespace(token="x"))
    monkeypatch.setattr(runner_update, "open_plan", lambda *a: None)
    request = {
        "source": str(tmp_path / "config"),
        "run_root": str(tmp_path),
        "update": True,
        "live": {**asdict(live), "catalog": catalog.model_dump(mode="json")},
    }
    assert reread_worker.run(request)["preview"] == {"included": False}


@pytest.mark.parametrize("reason", ["host", "lint", "preflight"])
def test_preview_reuses_preparation_refusals(monkeypatch, tmp_path, reason):
    from test_main_config import estate_file, live_of
    from dsl41 import main_config

    path = estate_file(tmp_path, lint='refuse = "error"\n')
    active = main_config.prepare(path)
    live = live_of(active)
    if reason == "host":
        path.write_text(path.read_text().replace("[engine]", '[engine]\nas_machine = ["other"]'))
    elif reason == "lint":
        (tmp_path / "a.jil").write_text(
            JOB.replace("insert_job: j", "insert_job: A") + "condition: s(MISSING)\n"
        )
    else:
        item = PreflightItem(severity="ERROR", code="synthetic", job="A", message="blocked")
        monkeypatch.setattr(reload_plan, "preflight_items", lambda *a: [item])
    candidate = main_config.prepare(path)
    with pytest.raises(RereadRefused) as caught:
        check_reload_candidate(candidate, live)
    view = reread_worker.preview(candidate, live)
    blocked = view["definition_changes"]["blocked"]
    reported = {*view["lint_messages"], *view["diagnostics"]}
    assert blocked
    assert all(reason in reported or reason in blocked for reason in caught.value.findings)
    with pytest.raises(RereadRefused) as prepared:
        reload_plan.reread(live, tmp_path / "plans", candidate=candidate)
    assert prepared.value.findings == caught.value.findings
    assert not (tmp_path / "plans").exists()


@pytest.mark.parametrize("reverse", [False, True])
def test_reason_for_attribute_addition_and_removal_stays_changed(reverse):
    text = "insert_machine: m\ntype: a\nnode_name: a\n\ninsert_job: j\njob_type: c\ncommand: true\nmachine: m\n"
    old, new = (
        lower_source(text),
        lower_source(text.replace("node_name: a", "node_name: a\ndescription: added")),
    )
    if reverse:
        old, new = new, old
    assert diff(old, new, {}, {}).changed[0].reason == "machine m changed"


def test_reason_keeps_main_equality_for_undefined_transitive_references_and_bool_int():
    text = "insert_machine: V\ntype: v\nmachine: ma\nfactor: 1\nmax_load: 1\n\ninsert_job: j\njob_type: c\ncommand: true\nmachine: V\n"
    old, new = lower_source(text), lower_source(text.replace("machine: ma", "machine: mb"))
    assert diff(old, new, {}, {}).changed[0].reason == "machine V changed"
    catalog = lower_source(JOB)
    assert diff(catalog, catalog, {"test": True}, {"test": 1}).changed[0].reason == ""


@pytest.mark.parametrize("distinct", [False, True])
def test_whole_difference_budget_for_5000_jobs_and_300_settings(monkeypatch, tmp_path, distinct):
    base = lower_source(JOB)
    old, new = base.model_copy(deep=True), base.model_copy(deep=True)
    template = base.jobs["j"]
    old.jobs.clear()
    new.jobs.clear()
    for index in range(5000):
        name = f"job_{index:04}"
        old.jobs[name] = template.model_copy(update={"name": name}, deep=True)
        new.jobs[name] = template.model_copy(update={"name": name}, deep=True)
        if distinct:
            for field in range(16):
                key = f"field_{field:02}"
                old.jobs[name].passthrough[key] = f"old-{index}-{field}-" + "x" * 200
                new.jobs[name].passthrough[key] = f"new-{index}-{field}-" + "y" * 200
    monkeypatch.setattr(reread_worker, "preflight_items", lambda *a: [])
    monkeypatch.setattr(reload_plan, "preflight_items", lambda *a: [])
    before = {"job_env": {f"V_{i:03}": f"old-secret-{i}" for i in range(300)}}
    after = {"job_env": {f"V_{i:03}": f"new-secret-{i}" for i in range(300)}}
    candidate = difference_estate(tmp_path, new, after)
    view = reread_worker.preview(
        candidate,
        Live(old, {**candidate.settings, **before}, 1, {}, launch=candidate.metadata["launch"]),
    )
    details = view["definition_changes"]
    wire = json.dumps(details).encode()
    assert len(wire) <= reread_worker.DIFFERENCE_BYTE_BUDGET
    assert len(json.dumps({"ok": True, "preview": view}).encode()) < LINE_LIMIT // 2
    assert all(not item["jobs"] for item in details["groups"] if item["scope"] == "settings")
    assert b"old-secret" not in wire and b"new-secret" not in wire
    assert bool(details["omitted"]) == distinct
    if distinct:
        from dsl41.cli_output import OutputStyle, reread_lines

        answer = {"preview": view, "live_rev": 1, "source": {"path": "/synthetic/estate.jil"}}
        for style in OutputStyle:
            assert f"{details['omitted']} more differences not shown" in "\n".join(
                reread_lines(answer, style, "dsl41 update")
            )


@pytest.mark.parametrize(
    "before,after",
    [
        ({}, {"job_env": {"NAME": "new-secret"}}),
        ({"job_env": {"NAME": "old-secret"}}, {}),
        ({"job_env": {"NAME": "old-secret"}}, {"job_env": {"NAME": "new-secret"}}),
    ],
)
def test_environment_introduction_removal_and_edit_expose_names_only(
    monkeypatch, tmp_path, before, after
):
    monkeypatch.setattr(reload_plan, "preflight_items", lambda *a: [])
    catalog = lower_source(JOB)
    candidate = difference_estate(tmp_path, catalog, after)
    live = Live(
        catalog,
        {**candidate.settings, "job_env": before.get("job_env", {})},
        1,
        {},
        launch=candidate.metadata["launch"],
    )
    details = reread_worker.definition_changes(candidate, live)
    assert details["settings_changed"]
    assert len(details["groups"]) == 1
    group = details["groups"][0]
    assert group["jobs"] == [] and "NAME" in group["field"]
    assert "secret" not in json.dumps(details)


def test_empty_applied_fingerprint_does_not_claim_estate_files_changed(tmp_path):
    from dsl41 import main_config
    from test_main_config import estate_file, live_of

    estate = main_config.prepare(estate_file(tmp_path))
    details = reread_worker.definition_changes(estate, live_of(estate), source_fingerprint={})
    assert details["sources_changed"] is None
