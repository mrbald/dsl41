"""The goldens' inputs and renderings (tests/oracle_inputs.py, DL-351).

The seeded estates are deterministic and carry every input family and
attribute the oracle reads; the switch matrix is each value of each switch
once, then the named pairs; the seal's `state` section is serialized as the
seal writes it and holds the fields the engine fills; equal stores render
the same whatever order their sets iterate or their rows were inserted in;
and a state the seal refuses renders as its refusal.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from collections.abc import Callable
from datetime import datetime

from estate_gen import Advance, SealPoint, seeded
from oracle_inputs import _Rows, seal_digest, seal_state_payload, sealed_state, switch_matrix
from test_seal_artifact import _seal

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, JobRuntime
from dsl41.runner import Engine
from dsl41.seal import SealedState
from dsl41.semantics import REGISTRY

T0 = datetime(2026, 7, 1, 8, 0)

#: the event kinds the generated scripts must carry (ss16's input families)
SCRIPT_KINDS = {
    "STATUS",
    "STARTJOB",
    "FORCE_STARTJOB",
    "KILLJOB",
    "ON_ICE",
    "OFF_ICE",
    "ON_HOLD",
    "OFF_HOLD",
    "ON_NOEXEC",
    "OFF_NOEXEC",
    "DISARM",
    "RELEASE_RESOURCE",
    "SET_GLOBAL",
}

_TWO_JOBS = (
    "insert_job: alpha\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: beta\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(alpha)\n"
)


# ------------------------------------------------------------- the inputs


def test_the_generator_is_deterministic_per_seed() -> None:
    assert seeded(7, 50) == seeded(7, 50)
    assert seeded(7, 50) != seeded(8, 50)


def test_a_seeded_estate_has_the_jobs_it_was_sized_for() -> None:
    sizes = [seeded(seed, 200, min_size=150).jobs for seed in range(10)]
    assert all(150 <= size <= 200 for size in sizes)
    assert all(1 <= seeded(seed, 5).jobs <= 5 for seed in range(30))


def test_a_share_of_the_generated_boxes_names_resources_after_the_script() -> None:
    """A small share of the boxes name resources (DL-346), so the
    sample reaches queued and unit-holding boxes. They are drawn after the
    script, so the jobs' draws and every input stay what they were, and a
    box's resources line is the last of its block."""
    sample = [seeded(seed, 50) for seed in range(30)]
    blocks = [block for case in sample for block in case.jil.split("\n\n")]
    boxes = [block for block in blocks if "job_type: b" in block]
    held = [block for block in boxes if "resources:" in block]
    assert 0 < len(held) < len(boxes) / 2
    assert all(block.splitlines()[-1].startswith("resources:") for block in held)


def test_the_generated_sample_carries_every_family() -> None:
    sample = [seeded(seed, 50) for seed in range(30)]
    kinds = {step.kind for case in sample for step in case.script if isinstance(step, Event)}
    assert kinds == SCRIPT_KINDS
    assert any(isinstance(step, Advance) for case in sample for step in case.script)
    assert any(isinstance(step, SealPoint) for case in sample for step in case.script)
    text = "\n".join(case.jil for case in sample)
    for attribute in (
        "job_type: b",
        "box_name:",
        "auto_hold: 1",
        "start_mins:",
        "box_success:",
        "box_failure:",
        "box_terminator: 1",
        "job_terminator: 1",
        "run_window:",
        "run_calendar:",
        "exclude_calendar:",
        "extended_calendar:",
        "calendar:",
        "timezone:",
        "must_start_times:",
        "must_complete_times:",
        "term_run_time:",
        "resources:",
        "max_load:",
        "job_load:",
        "insert_global:",
        "status: ON_ICE",
        "status: ON_HOLD",
        "status: ON_NOEXEC",
        "res_type: R",
        "res_type: D",
        "res_type: T",
        "FREE=",
    ):
        assert attribute in text, attribute
    # a condition on the job itself or on a later one: the loops
    forward = 0
    for block in text.split("\n\n"):
        own = re.search(r"insert_job: \w+_(\d+)", block)
        named = re.search(r"^condition: (.*)$", block, re.MULTILINE)
        if own and named:
            cited = re.findall(r"\b(?:job|box)_(\d+)", named.group(1))
            forward += any(int(index) >= int(own.group(1)) for index in cited)
    assert forward > 0
    for atom in ("s(", "f(", "d(", "n(", "t(", "e(", "value("):
        assert f"condition: {atom}" in text or f" {atom}" in text, atom


def test_the_switch_matrix_is_each_value_of_each_switch_once() -> None:
    matrix = switch_matrix()
    assert matrix[0] == {}
    expected = [
        (name, value)
        for name, switch in REGISTRY.items()
        for value in switch.values
        if value != switch.default
    ]
    singles = matrix[1 : 1 + len(expected)]
    assert [next(iter(entry.items())) for entry in singles] == expected
    assert all(len(entry) == 1 for entry in singles)
    # the named pairs: the wekr switch under each queued-recheck value that reads it
    assert matrix[1 + len(expected) :] == [
        {"wekr-first-week": "partial", "queued-recheck": "1"},
        {"wekr-first-week": "partial", "queued-recheck": "2"},
    ]


# ------------------------------------------------------------- the renderings


def test_the_seal_state_section_is_serialized_as_the_seal_writes_it() -> None:
    seal = _seal()
    assert seal_state_payload(seal.state) == seal.to_payload()["state"]


def _sealed_state_keywords(function: Callable[..., object]) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SealedState"
    ]
    assert len(calls) == 1, (
        f"{function.__qualname__} builds SealedState in {len(calls)} calls, not one:"
        " point this pin at the function that builds the seal's state now"
    )
    return {keyword.arg for keyword in calls[0].keywords if keyword.arg is not None}


def test_the_seal_state_fills_the_fields_the_engine_fills() -> None:
    fields = set(SealedState.model_fields)
    assert _sealed_state_keywords(Engine._snapshot) == fields
    assert _sealed_state_keywords(sealed_state) == fields


def _equal_rows_iterating_differently() -> tuple[JobRuntime, JobRuntime]:
    """Two equal rows whose `ran_members` iterate in different orders: a
    pair of names that collide in a small set's table, found in this
    process (the order depends on the hash seed)."""
    names = [f"job_{i:03d}" for i in range(200)]
    for a in names:
        for b in names:
            if a < b and list(frozenset([a, b])) != list(frozenset([b, a])):
                return JobRuntime(ran_members=frozenset([a, b])), JobRuntime(
                    ran_members=frozenset([b, a])
                )
    raise AssertionError("no colliding pair among 200 names")  # pragma: no cover


def test_equal_rows_render_the_same_whatever_order_their_sets_iterate() -> None:
    first, second = _equal_rows_iterating_differently()
    assert first == second
    assert first.model_dump(mode="json") != second.model_dump(mode="json")
    catalog = lower_source(_TWO_JOBS)
    one, other = Oracle(catalog), Oracle(catalog.model_copy(deep=True))
    one.store._jobs["alpha"] = first
    other.store._jobs["alpha"] = second
    assert _Rows().render(one) == _Rows().render(other)
    assert seal_digest(one, T0) == seal_digest(other, T0)


def test_rows_are_rendered_by_name_as_the_seal_writes_them() -> None:
    catalog = lower_source(_TWO_JOBS)
    one, other = Oracle(catalog), Oracle(catalog.model_copy(deep=True))
    other.store._jobs = dict(reversed(list(other.store._jobs.items())))
    assert list(other.store.job) != list(one.store.job)
    assert _Rows().render(one) == _Rows().render(other)


def test_an_unsealable_state_is_rendered_as_its_refusal() -> None:
    oracle = Oracle(lower_source(_TWO_JOBS))
    assert seal_digest(oracle, T0)[0].startswith("sha256:")
    oracle.store._enqueue_counter = -1
    found, payload = seal_digest(oracle, T0)
    assert payload is None
    assert found.startswith("unsealable: ValidationError")
