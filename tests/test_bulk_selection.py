"""Synthetic bulk values, pins, sizing and exact retry preparation."""

import copy
import json
from dataclasses import replace
from typing import get_args

import pytest
from hypothesis import given, strategies as st

from dsl41.bulk_selection import (
    BulkRequest,
    BulkVerb,
    ExpectationFailure,
    TargetExpectation,
    bulk_request,
    check_selection,
    expectation_failures,
)
from dsl41.cli_selection import prepare_bulk_request
from dsl41.ir import BoxLinkage, JobIR
from dsl41.job_selection import JobSelectors, SelectionSnapshot, selection_snapshot


def snapshot():
    catalog = {
        "root": JobIR(name="root", job_type="BOX", passthrough={"application": "A"}),
        "leaf": JobIR(name="leaf", job_type="CMD", box=BoxLinkage(box_name="root")),
    }
    return selection_snapshot(
        catalog,
        JobSelectors(application="A"),
        baseline_id="synthetic",
        applied_index=10,
        config_rev=2,
        job_revisions={"root": 4, "leaf": 9},
    )


def request():
    return bulk_request(
        snapshot(),
        request_id="synthetic-request",
        verb="KILLJOB",
        arguments={"override": True},
        claimed_actor="synthetic-actor",
    )


@pytest.mark.parametrize("verb", get_args(BulkVerb))
def test_all_job_verbs_have_one_request_with_every_target_and_pin(verb) -> None:
    arguments = {"status": "SUCCESS", "exit_code": 0} if verb == "CHANGE_STATUS" else {}
    value = bulk_request(snapshot(), request_id="r", verb=verb, arguments=arguments)
    body = value.as_request(version=4)
    assert body["cmd"] == "sendevent_bulk"
    assert body["verb"] == verb
    assert body["targets"] == ["root", "leaf"]
    assert body["expect"] == {"job:root": 4, "job:leaf": 9}
    assert body["config_rev"] == 2
    assert body["selectors"] == {"application": "A"}
    assert body["payload"] == arguments


@pytest.mark.parametrize(
    "selectors",
    [JobSelectors(), JobSelectors(application=""), JobSelectors(application="A", group="")],
)
def test_empty_bulk_selectors_refuse(selectors) -> None:
    with pytest.raises(ValueError, match="empty selectors"):
        replace(request(), selectors=selectors)


def test_zero_duplicate_unknown_verb_and_bad_expectations_refuse() -> None:
    value = request()
    with pytest.raises(ValueError, match="zero targets"):
        bulk_request(replace(snapshot(), jobs=()), request_id="r", verb="ON_HOLD", arguments={})
    with pytest.raises(ValueError, match="duplicate"):
        replace(value, targets=("root", "root"))
    for verb in ("SET_GLOBAL", "TIMER", "STATUS", "unknown"):
        with pytest.raises(ValueError, match="job-directed"):
            replace(value, verb=verb)
    for expect in (
        (),
        (TargetExpectation("root", 4),),
        (TargetExpectation("root", 4), TargetExpectation("root", 4)),
        (TargetExpectation("root", 4), TargetExpectation("ghost", 9)),
    ):
        with pytest.raises(ValueError, match="every target"):
            replace(value, expect=expect)
    for revision in (-1, True, 1.5):
        with pytest.raises(ValueError, match="revisions"):
            replace(
                value, expect=(TargetExpectation("root", revision), TargetExpectation("leaf", 9))
            )


def test_fingerprint_covers_all_semantics_but_not_request_id_or_framing() -> None:
    value = request()
    variants = [
        replace(value, baseline_id="another"),
        replace(value, config_rev=3),
        replace(value, selectors=JobSelectors(application="A", group="G")),
        replace(value, targets=("leaf", "root")),
        replace(value, verb="ON_HOLD"),
        replace(value, arguments_json='{"override":false}'),
        replace(value, expect=(TargetExpectation("root", 5), TargetExpectation("leaf", 9))),
    ]
    assert all(
        changed.fingerprint(actor="authenticated") != value.fingerprint(actor="authenticated")
        for changed in variants
    )
    assert replace(value, request_id="another-id").fingerprint(
        actor="authenticated"
    ) == value.fingerprint(actor="authenticated")
    assert replace(value, expect=tuple(reversed(value.expect))).fingerprint(
        actor="authenticated"
    ) == value.fingerprint(actor="authenticated")
    assert len(value.fingerprint(actor="authenticated")) == 64
    assert value.as_request(version=4)["v"] == 4
    assert value.as_request(version=5)["v"] == 5


@given(st.permutations(("status", "exit_code", "details")))
def test_fingerprint_is_stable_across_argument_key_order(keys) -> None:
    arguments = {"status": "SUCCESS", "exit_code": 0, "details": {"b": [1, True], "a": None}}
    first = bulk_request(snapshot(), request_id="r", verb="CHANGE_STATUS", arguments=arguments)
    second = bulk_request(
        snapshot(),
        request_id="r",
        verb="CHANGE_STATUS",
        arguments={key: arguments[key] for key in keys},
    )
    assert first.fingerprint(actor="authenticated") == second.fingerprint(actor="authenticated")
    assert first.frame(version=4, max_request_bytes=4096) == second.frame(
        version=4, max_request_bytes=4096
    )


def test_frozen_arguments_and_wire_copies_do_not_change_retry() -> None:
    arguments = {"override": True, "nested": {"flags": [1]}}
    value = bulk_request(snapshot(), request_id="r", verb="KILLJOB", arguments=arguments)
    original = value.frame(version=4, max_request_bytes=4096)
    arguments["nested"]["flags"].append(2)
    copy = value.arguments
    copy["override"] = False
    wire = value.as_request(version=4)
    wire["targets"].append("ghost")
    assert value.frame(version=4, max_request_bytes=4096) == original
    assert value.arguments["override"] is True


def test_size_limit_counts_full_encoded_frame_and_refuses_without_chunking() -> None:
    value = replace(request(), claimed_actor="非ASCII")
    encoded = value.frame(version=4, max_request_bytes=4096)
    assert value.frame(version=4, max_request_bytes=len(encoded)) == encoded
    with pytest.raises(ValueError, match="oversized"):
        value.frame(version=4, max_request_bytes=len(encoded) - 1)
    for invalid in (0, -1, True):
        with pytest.raises(ValueError, match="positive integer"):
            value.frame(version=4, max_request_bytes=invalid)


def test_cli_preview_is_complete_and_is_the_frozen_send_frame() -> None:
    prepared = prepare_bulk_request(
        snapshot(),
        request_id="r",
        verb="KILLJOB",
        arguments={"override": True},
        version=4,
        max_request_bytes=4096,
    )
    assert (prepared.preview + "\n").encode() == prepared.wire_bytes
    body = json.loads(prepared.preview)
    assert body == prepared.request.as_request(version=4)
    assert body["payload"] == {"override": True}
    assert body["expect"] == {"job:root": 4, "job:leaf": 9}
    assert body["baseline_id"] == "synthetic"
    for conflict in ({"scalar_expect": 0}, {"job": "leaf"}):
        with pytest.raises(ValueError):
            prepare_bulk_request(
                snapshot(),
                request_id="r",
                verb="KILLJOB",
                arguments={},
                version=4,
                max_request_bytes=4096,
                **conflict,
            )
    with pytest.raises(ValueError, match="oversized"):
        prepare_bulk_request(
            snapshot(), request_id="r", verb="KILLJOB", arguments={}, version=4, max_request_bytes=1
        )


def test_membership_configuration_and_all_expectations_are_separate_checks() -> None:
    value = request()
    current = snapshot()
    check_selection(value, current)
    for changed in (
        replace(current, config_rev=3),
        replace(current, baseline_id="another"),
        replace(current, jobs=current.jobs[:1]),
        replace(current, jobs=tuple(reversed(current.jobs))),
        replace(current, selectors=JobSelectors(group="G")),
    ):
        with pytest.raises(ValueError, match="changed"):
            check_selection(value, changed)
    assert expectation_failures(value, {"root": 4, "leaf": 9}) == ()
    assert expectation_failures(value, {"root": 5}) == (
        ExpectationFailure("root", 4, 5),
        ExpectationFailure("leaf", 9, None),
    )
    assert value.targets == ("root", "leaf")
    assert value.expect == (TargetExpectation("root", 4), TargetExpectation("leaf", 9))


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": "engine:1"},
        {"request_id": ""},
        {"baseline_id": ""},
        {"config_rev": True},
        {"arguments_json": '{"job":"root"}'},
        {"arguments_json": "[]"},
        {"arguments_json": '{"value":1.5}'},
    ],
)
def test_malformed_request_values_refuse(changes) -> None:
    with pytest.raises(ValueError):
        replace(request(), **changes)


scalar_text = st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=16)
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | scalar_text,
    lambda child: st.lists(child, max_size=4) | st.dictionaries(scalar_text, child, max_size=4),
    max_leaves=12,
)


@given(
    payload=json_values,
    actor=st.none() | scalar_text,
    claim=st.none() | scalar_text,
    root_rev=st.integers(min_value=0, max_value=1000000),
    leaf_rev=st.integers(min_value=0, max_value=1000000),
    label=st.text(alphabet="Aa09_.-", min_size=1, max_size=16),
)
def test_snapshot_and_bulk_json_round_trips_preserve_fingerprint(
    payload, actor, claim, root_rev, leaf_rev, label
) -> None:
    catalog = {
        "root": JobIR(name="root", job_type="BOX", passthrough={"application": label}),
        "leaf": JobIR(name="leaf", job_type="CMD", box=BoxLinkage(box_name="root")),
    }
    original_snapshot = selection_snapshot(
        catalog,
        JobSelectors(application=label),
        baseline_id="synthetic",
        applied_index=10,
        config_rev=2,
        job_revisions={"root": root_rev, "leaf": leaf_rev},
    )
    restored_snapshot = SelectionSnapshot.from_dict(
        json.loads(json.dumps(original_snapshot.as_dict(), sort_keys=True))
    )
    assert restored_snapshot == original_snapshot
    original = bulk_request(
        original_snapshot,
        request_id="r",
        verb="KILLJOB",
        arguments={"override": True, "extra": payload},
        claimed_actor=claim,
    )
    frame = original.frame(version=4, max_request_bytes=1000000)
    restored = BulkRequest.from_request(json.loads(frame))
    reordered = BulkRequest.from_request(json.loads(json.dumps(json.loads(frame), sort_keys=True)))
    assert restored == reordered == original
    assert restored.fingerprint(actor=actor) == original.fingerprint(actor=actor)
    assert restored.frame(version=4, max_request_bytes=1000000) == frame
    assert (
        bulk_request(
            restored_snapshot,
            request_id="r",
            verb="KILLJOB",
            arguments={"override": True, "extra": payload},
            claimed_actor=claim,
        )
        == original
    )


@pytest.mark.parametrize(
    "problem",
    [
        "missing_pin",
        "extra_pin",
        "bare_pin",
        "bool_pin",
        "missing_field",
        "extra_field",
        "not_bulk",
        "bad_version",
        "targets_string",
        "selector_typo",
    ],
)
def test_bulk_decoder_refuses_malformed_wire_values(problem) -> None:
    value = copy.deepcopy(request().as_request(version=4))
    if problem == "missing_pin":
        del value["expect"]["job:root"]
    elif problem == "extra_pin":
        value["expect"]["job:ghost"] = 0
    elif problem == "bare_pin":
        value["expect"]["root"] = value["expect"].pop("job:root")
    elif problem == "bool_pin":
        value["expect"]["job:root"] = True
    elif problem == "missing_field":
        del value["targets"]
    elif problem == "extra_field":
        value["unknown"] = 1
    elif problem == "not_bulk":
        value["cmd"] = "sendevent"
    elif problem == "bad_version":
        value["v"] = True
    elif problem == "targets_string":
        value["targets"] = "root"
    else:
        value["selectors"]["aplication"] = value["selectors"].pop("application")
    with pytest.raises(ValueError):
        BulkRequest.from_request(value)


def test_absent_and_null_actor_claims_decode_to_the_same_request() -> None:
    value = replace(request(), claimed_actor=None)
    with_null = value.as_request(version=4)
    absent = {key: item for key, item in with_null.items() if key != "claimed_actor"}
    assert BulkRequest.from_request(with_null) == BulkRequest.from_request(absent) == value


def test_fingerprint_requires_server_resolved_actor_and_ignores_the_claim() -> None:
    value = request()
    forged = replace(value, claimed_actor="forged-claim")
    assert value.fingerprint(actor="authenticated") == forged.fingerprint(actor="authenticated")
    assert value.fingerprint(actor="authenticated") != value.fingerprint(actor="second-principal")
    assert value.fingerprint(actor=None) == forged.fingerprint(actor=None)
    with pytest.raises(TypeError):
        value.fingerprint()
    with pytest.raises(ValueError, match="actor"):
        value.fingerprint(actor=42)
