"""The transition coverage gate, scripts/transition_coverage.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from dsl41.state_machine import StateMachine, Transition

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "transition_coverage.py"


def _load() -> ModuleType:
    """scripts/ is not a package, so the test loads the script by path."""
    spec = importlib.util.spec_from_file_location("transition_coverage", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["transition_coverage"] = module
    spec.loader.exec_module(module)
    return module


tc = _load()

T1 = Transition("demo.01", frozenset({"a", "b"}), "go", "c")
T2 = Transition("demo.02", frozenset({"c"}), "back", frozenset({"a", "b"}))
T3 = Transition("demo.03", frozenset({"a"}), "later", "b", mark="spec-only")
T4 = Transition("demo.04", frozenset({"b"}), "never", "a", mark="unreachable")
MACHINE = StateMachine(
    name="demo",
    states=frozenset({"a", "b", "c"}),
    initial="a",
    finals=frozenset(),
    transitions=(T1, T2, T3, T4),
)


def _hit(tid: str, old: str, new: str = "c") -> dict[str, str]:
    return {"machine": "demo", "id": tid, "old": old, "new": new}


ALL_UNMARKED = [_hit("demo.01", "a"), _hit("demo.01", "b"), _hit("demo.02", "c", "a")]


def test_every_unmarked_transition_hit_passes() -> None:
    lines, status = tc.report([MACHINE], ALL_UNMARKED)
    assert status == 0
    assert lines[0] == "demo: 2/2"
    assert "Transitions with no hit:" not in lines
    assert "Sources never exercised (report only):" not in lines


def test_a_missed_transition_fails_and_is_listed() -> None:
    lines, status = tc.report([MACHINE], [_hit("demo.01", "a"), _hit("demo.01", "b")])
    assert status == 1
    assert lines[0] == "demo: 1/2"
    assert "Transitions with no hit:" in lines
    assert "demo demo.02 {c} --back--> {a, b}" in lines


def test_a_fixed_target_is_shown_in_the_miss_line() -> None:
    lines, _ = tc.report([MACHINE], [])
    assert "demo demo.01 {a, b} --go--> c" in lines


def test_marked_transitions_are_listed_and_excluded() -> None:
    lines, status = tc.report([MACHINE], ALL_UNMARKED)
    assert status == 0
    assert "Marked transitions (excluded):" in lines
    assert "demo demo.03 {a} --later--> b [spec-only]" in lines
    assert "demo demo.04 {b} --never--> a [unreachable]" in lines


def test_a_hit_on_a_marked_transition_does_not_count() -> None:
    lines, _ = tc.report([MACHINE], [*ALL_UNMARKED, _hit("demo.03", "a", "b")])
    assert lines[0] == "demo: 2/2"


def test_a_source_never_exercised_is_reported_but_does_not_fail() -> None:
    lines, status = tc.report([MACHINE], [_hit("demo.01", "a"), _hit("demo.02", "c", "a")])
    assert status == 0
    assert lines[lines.index("Sources never exercised (report only):") + 1 :] == [
        "demo demo.01 from b"
    ]


def test_an_unhit_transition_is_not_repeated_in_the_source_view() -> None:
    lines, _ = tc.report([MACHINE], [_hit("demo.01", "a")])
    assert "Sources never exercised (report only):" in lines
    assert not any(line.startswith("demo demo.02 from") for line in lines)


def test_hits_of_another_machine_do_not_count() -> None:
    other = {"machine": "other", "id": "other.01", "old": "a", "new": "c"}
    _, status = tc.report([MACHINE], [other])
    assert status == 1


def test_an_empty_registry_passes() -> None:
    assert tc.report([], []) == ([], 0)


def test_main_exits_2_when_the_hits_file_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert tc.main(tmp_path / "missing.json", [MACHINE]) == 2
    assert "missing" in capsys.readouterr().err


def test_main_reads_the_file_and_prints_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "hits.json"
    path.write_text(json.dumps({"hits": ALL_UNMARKED, "violations": []}), encoding="utf-8")
    assert tc.main(path, [MACHINE]) == 0
    assert capsys.readouterr().out.startswith("demo: 2/2")
    path.write_text(json.dumps({"hits": [], "violations": []}), encoding="utf-8")
    assert tc.main(path, [MACHINE]) == 1


def test_main_reads_the_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "hits.json"
    path.write_text(json.dumps({"hits": [], "violations": []}), encoding="utf-8")
    monkeypatch.setattr(tc, "MACHINES", ())
    assert tc.main(path) == 0
    monkeypatch.setattr(tc, "MACHINES", (MACHINE,))
    assert tc.main(path) == 1
    assert capsys.readouterr().out.count("demo: 0/2") == 1


def test_a_recorded_violation_fails_the_gate_and_is_listed() -> None:
    violation = {
        "machine": "demo",
        "id": "demo.01",
        "old": "c",
        "new": "c",
        "reason": "c is no source",
    }
    lines, status = tc.report([MACHINE], ALL_UNMARKED, [violation])
    assert status == 1
    assert lines[lines.index("Violations recorded:") + 1] == "demo demo.01: c -> c: c is no source"


def test_no_violation_leaves_the_status_alone() -> None:
    assert tc.report([MACHINE], ALL_UNMARKED, [])[1] == 0


def test_main_fails_on_violations_in_the_file(tmp_path: Path) -> None:
    violation = {"machine": "m", "id": "?", "old": "", "new": "", "reason": "torn"}
    path = tmp_path / "hits.json"
    path.write_text(json.dumps({"hits": ALL_UNMARKED, "violations": [violation]}), encoding="utf-8")
    assert tc.main(path, [MACHINE]) == 1


def test_a_hit_on_an_unreachable_transition_fails_and_is_listed() -> None:
    lines, status = tc.report([MACHINE], [*ALL_UNMARKED, _hit("demo.04", "b", "a")])
    assert status == 1
    heading = "Marked but hit (the mark is stale or wrong):"
    assert lines[lines.index(heading) + 1] == "demo demo.04 {b} --never--> a [unreachable]"


def test_a_hit_on_a_spec_only_transition_fails_and_is_listed() -> None:
    """Code now takes the transition, so the spec-only mark is stale."""
    lines, status = tc.report([MACHINE], [*ALL_UNMARKED, _hit("demo.03", "a", "b")])
    assert status == 1
    heading = "Marked but hit (the mark is stale or wrong):"
    assert lines[lines.index(heading) + 1] == "demo demo.03 {a} --later--> b [spec-only]"
