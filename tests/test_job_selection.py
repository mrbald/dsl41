"""Synthetic catalog selection, inheritance and query preparation."""

import copy
import json
from dataclasses import FrozenInstanceError

import pytest
from hypothesis import given, strategies as st

from dsl41.cli_selection import query_selectors
from dsl41.ir import BoxLinkage, JobIR, lower_source
from dsl41.job_selection import (
    EffectiveLabel,
    JobSelectors,
    SelectionSnapshot,
    ordered_targets,
    resolve_effective_labels,
    selection_snapshot,
)


def synthetic_catalog() -> dict[str, JobIR]:
    return {
        "z_root": JobIR(
            name="z_root", job_type="BOX", passthrough={"application": '"Pay"', "group": "Ops"}
        ),
        "a_box": JobIR(
            name="a_box",
            job_type="BOX",
            box=BoxLinkage(box_name="z_root"),
            passthrough={"group": "Batch"},
        ),
        "b_leaf": JobIR(name="b_leaf", job_type="CMD", box=BoxLinkage(box_name="a_box")),
        "a_leaf": JobIR(
            name="a_leaf",
            job_type="CMD",
            box=BoxLinkage(box_name="a_box"),
            passthrough={"Application": "pay"},
        ),
        "c_leaf": JobIR(name="c_leaf", job_type="CMD", box=BoxLinkage(box_name="z_root")),
        "plain": JobIR(name="plain", job_type="CMD"),
        "other": JobIR(
            name="other", job_type="CMD", passthrough={"application": "Pay", "group": "Other"}
        ),
    }


def test_nested_labels_override_independently_and_keep_origins() -> None:
    catalog = synthetic_catalog()
    before = {name: job.model_dump() for name, job in catalog.items()}
    labels = resolve_effective_labels(catalog)
    assert labels["b_leaf"].application == EffectiveLabel("Pay", "z_root")
    assert labels["b_leaf"].group == EffectiveLabel("Batch", "a_box")
    assert labels["a_leaf"].application == EffectiveLabel("pay", "a_leaf")
    assert labels["a_leaf"].group == EffectiveLabel("Batch", "a_box")
    assert labels["c_leaf"].group == EffectiveLabel("Ops", "z_root")
    assert labels["plain"].application == EffectiveLabel()
    assert labels["plain"].group == EffectiveLabel()
    assert before == {name: job.model_dump() for name, job in catalog.items()}


@pytest.mark.parametrize(
    ("selectors", "expected"),
    [
        (JobSelectors(application="Pay"), {"z_root", "a_box", "b_leaf", "c_leaf", "other"}),
        (JobSelectors(application="pay"), {"a_leaf"}),
        (JobSelectors(application="PAY"), set()),
        (JobSelectors(application="P*"), set()),
        (JobSelectors(application=" Pay"), set()),
        (JobSelectors(application="Pay", group="Batch"), {"a_box", "b_leaf"}),
        (JobSelectors(application="Pay", group="Other"), {"other"}),
        (JobSelectors(application="", group=""), {"plain"}),
        (JobSelectors(group="batch"), set()),
    ],
)
def test_exact_case_sensitive_intersection_and_unlabelled(selectors, expected) -> None:
    labels = resolve_effective_labels(synthetic_catalog())
    assert {name for name, value in labels.items() if selectors.matches(value)} == expected


@pytest.mark.parametrize("empty_value", ["", '""', "  "])
def test_unlabelled_inheritance_and_empty_declarations_inherit(empty_value) -> None:
    catalog = synthetic_catalog()
    catalog["plain_box"] = JobIR(name="plain_box", job_type="BOX")
    catalog["plain_child"] = JobIR(
        name="plain_child", job_type="CMD", box=BoxLinkage(box_name="plain_box")
    )
    catalog["empty_box"] = JobIR(
        name="empty_box",
        job_type="BOX",
        box=BoxLinkage(box_name="z_root"),
        passthrough={"application": empty_value, "group": empty_value},
    )
    catalog["empty_child"] = JobIR(
        name="empty_child", job_type="CMD", box=BoxLinkage(box_name="empty_box")
    )
    catalog["empty_root"] = JobIR(
        name="empty_root",
        job_type="CMD",
        passthrough={"application": empty_value, "group": empty_value},
    )
    labels = resolve_effective_labels(catalog)
    assert labels["plain_child"].application == EffectiveLabel()
    for name in ("empty_box", "empty_child"):
        assert labels[name].application == EffectiveLabel("Pay", "z_root")
        assert labels[name].group == EffectiveLabel("Ops", "z_root")
        assert not JobSelectors(application="").matches(labels[name])
    assert labels["empty_root"].application == EffectiveLabel()
    assert labels["empty_root"].group == EffectiveLabel()
    assert JobSelectors(application="", group="").matches(labels["empty_root"])
    assert catalog["empty_box"].passthrough == {"application": empty_value, "group": empty_value}


def test_lowering_carries_empty_labels_and_resolver_inherits() -> None:
    catalog = lower_source(
        "insert_job: root job_type: b\napplication: Pay\ngroup: Ops\n"
        'insert_job: child job_type: b\nbox_name: root\napplication: ""\ngroup: ""\n'
    )
    assert catalog.jobs["child"].passthrough == {"application": '""', "group": '""'}
    labels = resolve_effective_labels(catalog.jobs)
    assert labels["child"].application == EffectiveLabel("Pay", "root")
    assert labels["child"].group == EffectiveLabel("Ops", "root")


@given(st.permutations(tuple(synthetic_catalog())))
def test_order_and_inheritance_do_not_depend_on_catalog_insertion_order(names) -> None:
    catalog = synthetic_catalog()
    shuffled = {name: catalog[name] for name in names}
    assert ordered_targets(shuffled, names) == (
        "other",
        "plain",
        "z_root",
        "a_box",
        "a_leaf",
        "b_leaf",
        "c_leaf",
    )
    assert resolve_effective_labels(shuffled)["b_leaf"].application == EffectiveLabel(
        "Pay", "z_root"
    )


def test_subset_order_uses_unselected_ancestors() -> None:
    catalog = synthetic_catalog()
    assert ordered_targets(catalog, ["c_leaf", "b_leaf", "a_box", "z_root"]) == (
        "z_root",
        "a_box",
        "b_leaf",
        "c_leaf",
    )
    assert ordered_targets(catalog, ["c_leaf", "b_leaf"]) == ("b_leaf", "c_leaf")
    assert ordered_targets(catalog, []) == ()
    with pytest.raises(ValueError, match="duplicate"):
        ordered_targets(catalog, ["a_leaf", "a_leaf"])
    with pytest.raises(ValueError, match="outside"):
        ordered_targets(catalog, ["ghost"])


@pytest.mark.parametrize("problem", ["missing", "cycle", "not_box", "key"])
def test_invalid_catalog_ancestry_is_not_silently_discarded(problem) -> None:
    catalog = synthetic_catalog()
    if problem == "missing":
        del catalog["z_root"]
    elif problem == "cycle":
        catalog["z_root"].box.box_name = "a_box"
    elif problem == "not_box":
        catalog["z_root"].box.box_name = "plain"
    else:
        catalog["mismatch"] = catalog.pop("plain")
    with pytest.raises(ValueError):
        resolve_effective_labels(catalog)


def test_snapshot_freezes_pins_labels_order_and_excludes_ghosts() -> None:
    catalog = synthetic_catalog()
    revisions = {name: 5 for name in catalog} | {"ghost": 99}
    snapshot = selection_snapshot(
        catalog,
        JobSelectors(application="Pay", group="Batch"),
        baseline_id="synthetic",
        applied_index=8,
        config_rev=3,
        job_revisions=revisions,
    )
    assert snapshot.targets == ("a_box", "b_leaf")
    assert list(snapshot.by_name()) == ["a_box", "b_leaf"]
    assert snapshot.catalog_count == 7
    assert (snapshot.baseline_id, snapshot.applied_index, snapshot.config_rev) == (
        "synthetic",
        8,
        3,
    )
    assert [job.state_rev for job in snapshot.jobs] == [5, 5]
    catalog["z_root"].passthrough["application"] = "Changed"
    revisions["a_box"] = 6
    assert snapshot.jobs[0].labels.application.value == "Pay"
    assert snapshot.jobs[0].state_rev == 5
    with pytest.raises(FrozenInstanceError):
        snapshot.config_rev = 4


def test_empty_query_succeeds_and_missing_revision_is_explicit() -> None:
    catalog = synthetic_catalog()
    snapshot = selection_snapshot(
        catalog,
        JobSelectors(application="absent"),
        baseline_id="s",
        applied_index=0,
        config_rev=0,
        job_revisions={},
    )
    assert snapshot.by_name() == {}
    assert snapshot.targets == ()
    with pytest.raises(ValueError, match="missing revision"):
        selection_snapshot(
            catalog,
            JobSelectors(),
            baseline_id="s",
            applied_index=0,
            config_rev=0,
            job_revisions={},
        )
    all_jobs = selection_snapshot(
        catalog,
        JobSelectors(),
        baseline_id="s",
        applied_index=0,
        config_rev=0,
        job_revisions={name: 0 for name in catalog},
    )
    assert len(all_jobs.jobs) == len(catalog)


@pytest.mark.parametrize("revision", [-1, True, 1.5])
def test_bad_revisions_refuse(revision) -> None:
    with pytest.raises(ValueError, match="revisions"):
        selection_snapshot(
            synthetic_catalog(),
            JobSelectors(application="pay"),
            baseline_id="s",
            applied_index=0,
            config_rev=0,
            job_revisions={"a_leaf": revision},
        )


def test_query_selector_scope_and_empty_value() -> None:
    assert query_selectors("spec", application="").application == ""
    assert query_selectors("status", application="Pay", group="Ops").active
    assert not query_selectors("catalog").active
    for verb in ("catalog", "global", "resources", "triggers"):
        with pytest.raises(ValueError, match="only status and spec"):
            query_selectors(verb, group="")
    with pytest.raises(ValueError, match="mutually exclusive"):
        query_selectors("spec", job="a_leaf", application="")


def test_depth_first_order_keeps_prefix_siblings_after_the_whole_subtree() -> None:
    catalog = {
        "a": JobIR(name="a", job_type="BOX"),
        "a-x": JobIR(name="a-x", job_type="CMD"),
        "b": JobIR(name="b", job_type="BOX", box=BoxLinkage(box_name="a")),
        "c": JobIR(name="c", job_type="BOX", box=BoxLinkage(box_name="b")),
        "d": JobIR(name="d", job_type="BOX", box=BoxLinkage(box_name="c")),
        "e": JobIR(name="e", job_type="CMD", box=BoxLinkage(box_name="d")),
    }
    assert ordered_targets(catalog, reversed(catalog)) == ("a", "b", "c", "d", "e", "a-x")
    assert ordered_targets(catalog, ("a-x", "e")) == ("e", "a-x")


def test_snapshot_json_codec_preserves_empty_selectors_and_unlabelled_origins() -> None:
    catalog = synthetic_catalog()
    value = selection_snapshot(
        catalog,
        JobSelectors(application="", group=""),
        baseline_id="s",
        applied_index=8,
        config_rev=3,
        job_revisions={name: 0 for name in catalog},
    )
    encoded = json.loads(json.dumps(value.as_dict(), sort_keys=True))
    restored = SelectionSnapshot.from_dict(encoded)
    assert restored == value
    assert restored.jobs[0].labels.application == EffectiveLabel()
    assert restored.selectors == JobSelectors("", "")
    encoded["jobs"].clear()
    assert restored.targets == ("plain",)


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "extra",
        "jobs_object",
        "revision_bool",
        "empty_label",
        "missing_origin",
        "extra_selector",
    ],
)
def test_snapshot_decoder_rejects_malformed_values(problem) -> None:
    catalog = synthetic_catalog()
    snapshot = selection_snapshot(
        catalog,
        JobSelectors(application="Pay"),
        baseline_id="s",
        applied_index=8,
        config_rev=3,
        job_revisions={name: 1 for name in catalog},
    )
    value = copy.deepcopy(snapshot.as_dict())
    if problem == "missing":
        del value["baseline_id"]
    elif problem == "extra":
        value["unknown"] = 1
    elif problem == "jobs_object":
        value["jobs"] = {}
    elif problem == "revision_bool":
        value["jobs"][0]["state_rev"] = True
    elif problem == "empty_label":
        value["jobs"][0]["labels"]["application"]["value"] = ""
    elif problem == "missing_origin":
        del value["jobs"][0]["labels"]["application"]["origin"]
    else:
        value["selectors"]["unknown"] = "A"
    with pytest.raises(ValueError):
        SelectionSnapshot.from_dict(value)
