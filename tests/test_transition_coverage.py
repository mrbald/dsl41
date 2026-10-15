"""The transition coverage gate, scripts/transition_coverage.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from dsl41.state_machine import Region, Row, State, Statechart, coverage_items

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

# ---- statecharts (docs/statechart-engine.md §13)

CHART = Statechart(
    "lamp",
    State(
        "lamp",
        regions=(Region("main", "off", (State("off"), State("on", defer=frozenset({"x"})))),),
    ),
    (
        Row("lamp.01", "off", "switch", "on", guard="power", false_unreachable="mains"),
        Row("lamp.02", "on", "switch", "off", guard="cool"),
        Row("lamp.03", "lamp", "x", kind="ignored"),
    ),
    clock="logical",
    defer_bound=1,
    overflow="stop",
)
ALL_CHART = [
    {"machine": "lamp", "kind": kind, "id": ident, "detail": detail}
    for kind, ident, detail in [
        ("row", "lamp.01", ""),
        ("row", "lamp.02", ""),
        ("row", "lamp.03", ""),
        ("guard", "lamp.01", "true"),
        ("guard", "lamp.02", "true"),
        ("guard", "lamp.02", "false"),
        ("defer", "on", "x"),
        ("release", "on", "x"),
    ]
]


def test_a_statechart_with_every_item_hit_passes() -> None:
    lines, status = tc.report([CHART], ALL_CHART)
    assert status == 0
    assert lines == ["lamp: 8/8"]


def test_a_statechart_miss_in_any_class_fails_and_is_listed() -> None:
    for missing in ALL_CHART:
        lines, status = tc.report([CHART], [h for h in ALL_CHART if h != missing])
        assert status == 1
        assert lines[0] == "lamp: 7/8"
        wanted = " ".join(
            p for p in ("lamp", missing["kind"], missing["id"], missing["detail"]) if p
        )
        assert lines[lines.index("Transitions with no hit:") + 1] == wanted


def test_a_forbidden_guard_outcome_that_was_hit_fails() -> None:
    hit = {"machine": "lamp", "kind": "guard", "id": "lamp.01", "detail": "false"}
    lines, status = tc.report([CHART], [*ALL_CHART, hit])
    assert status == 1
    heading = "Marked but hit (the mark is stale or wrong):"
    assert lines[lines.index(heading) + 1] == "lamp guard lamp.01 false"


def test_hits_of_another_machine_do_not_count() -> None:
    renamed = [{**h, "machine": "other"} for h in ALL_CHART]
    lines, status = tc.report([CHART], renamed)
    assert status == 1 and lines[0] == "lamp: 0/8"


def test_an_empty_registry_passes() -> None:
    assert tc.report([], []) == ([], 0)


def test_main_exits_2_when_the_hits_file_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert tc.main(tmp_path / "missing.json", [CHART]) == 2
    assert "missing" in capsys.readouterr().err


def test_main_reads_the_file_and_prints_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "hits.json"
    path.write_text(json.dumps({"hits": ALL_CHART}), encoding="utf-8")
    assert tc.main(path, [CHART]) == 0
    assert capsys.readouterr().out.startswith("lamp: 8/8")
    path.write_text(json.dumps({"hits": []}), encoding="utf-8")
    assert tc.main(path, [CHART]) == 1


def test_main_reads_the_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "hits.json"
    path.write_text(json.dumps({"hits": []}), encoding="utf-8")
    monkeypatch.setattr(tc, "MACHINES", ())
    assert tc.main(path) == 0
    monkeypatch.setattr(tc, "MACHINES", (CHART,))
    assert tc.main(path) == 1
    assert capsys.readouterr().out.count("lamp: 0/8") == 1


#: The lamp with a spec-only row: a dimmer no code turns yet, one guarded and one not.
MARKED = replace(
    CHART,
    rows=(
        *CHART.rows,
        Row("lamp.04", "on", "dim", "off", guard="cool", mark="spec-only"),
        Row("lamp.05", "off", "dim", mark="spec-only", kind="ignored"),
    ),
)


def test_a_marked_row_is_listed_never_counted_and_fails_when_hit() -> None:
    """A row's mark is a table transition's mark (DL-333): the row and its
    guard's true outcome are listed, not counted, and a hit on either fails.
    Its guard's false outcome is the cell another row takes: neither required
    nor forbidden (DL-353)."""
    required, forbidden = coverage_items(MARKED)
    assert ("row", "lamp.04", "") not in required
    assert ("guard", "lamp.04", "false") not in required
    assert forbidden == [
        ("row", "lamp.04", ""),
        ("guard", "lamp.04", "true"),
        ("row", "lamp.05", ""),
        ("guard", "lamp.01", "false"),
    ]
    lines, status = tc.report([MARKED], ALL_CHART)
    assert status == 0 and lines[0] == "lamp: 8/8"
    heading = "Marked transitions (excluded):"
    assert lines[lines.index(heading) + 1 :][:2] == [
        "lamp lamp.04 {on} --dim--> off [spec-only]",
        "lamp lamp.05 {off} --dim--> ignored [spec-only]",
    ]
    hit = {"machine": "lamp", "kind": "row", "id": "lamp.04", "detail": ""}
    lines, status = tc.report([MARKED], [*ALL_CHART, hit])
    assert status == 1
    heading = "Marked but hit (the mark is stale or wrong):"
    assert lines[lines.index(heading) + 1] == "lamp row lamp.04"
    false = {"machine": "lamp", "kind": "guard", "id": "lamp.04", "detail": "false"}
    assert tc.report([MARKED], [*ALL_CHART, false])[1] == 0
    true = {**false, "detail": "true"}
    lines, status = tc.report([MARKED], [*ALL_CHART, true])
    assert status == 1 and lines[lines.index(heading) + 1] == "lamp guard lamp.04 true"
