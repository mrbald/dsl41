"""The branch-only coverage report, scripts/branch_coverage.py (DL-265).

The cases feed a synthetic coverage JSON, so they test the arithmetic and the
sort order and never start coverage itself.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "branch_coverage.py"


def _load() -> ModuleType:
    """scripts/ is not a package, so the test loads the script by path."""
    spec = importlib.util.spec_from_file_location("branch_coverage", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["branch_coverage"] = module
    spec.loader.exec_module(module)
    return module


branch_coverage = _load()


def _entry(covered: int, total: int) -> dict[str, object]:
    # statement counts are noise here: the report must read branches only
    return {"summary": {"covered_branches": covered, "num_branches": total, "percent_covered": 1}}


SYNTHETIC = {
    "files": {
        "src/dsl41/full.py": _entry(10, 10),
        "src/dsl41/b_gap.py": _entry(6, 10),
        "src/dsl41/big.py": _entry(60, 100),
        "src/dsl41/a_gap.py": _entry(6, 10),
        "src/dsl41/none.py": _entry(0, 0),
    },
    "totals": {"covered_branches": 1, "num_branches": 1},
}


def test_rows_sort_by_missed_branches_then_path() -> None:
    rows = branch_coverage.branch_rows(SYNTHETIC)
    assert [path for path, _, _ in rows] == [
        "src/dsl41/big.py",
        "src/dsl41/a_gap.py",
        "src/dsl41/b_gap.py",
        "src/dsl41/full.py",
        "src/dsl41/none.py",
    ]
    assert rows[0] == ("src/dsl41/big.py", 60, 100)


def test_total_is_the_sum_of_the_modules_not_the_file_totals() -> None:
    lines = branch_coverage.render(branch_coverage.branch_rows(SYNTHETIC)).splitlines()
    total = lines[-1].split()
    assert total[:4] == ["TOTAL", "82", "130", "48"]
    assert total[4] == "63.08%"


def test_module_line_shows_percent_and_a_module_with_no_branches_is_not_a_division_error() -> None:
    text = branch_coverage.render(branch_coverage.branch_rows(SYNTHETIC))
    big = next(line for line in text.splitlines() if line.startswith("src/dsl41/big.py"))
    assert big.split()[1:] == ["60", "100", "40", "60.00%"]
    none = next(line for line in text.splitlines() if line.startswith("src/dsl41/none.py"))
    assert none.split()[1:4] == ["0", "0", "0"]
    assert none.split()[-1] == "n/a"


def test_empty_report_prints_a_zero_total() -> None:
    text = branch_coverage.render(branch_coverage.branch_rows({"files": {}}))
    assert text.splitlines()[-1].split()[:4] == ["TOTAL", "0", "0", "0"]


def test_main_prints_the_table_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(branch_coverage, "coverage_json", lambda: SYNTHETIC)
    assert branch_coverage.main() == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("module")
    assert out.splitlines()[-1].startswith("TOTAL")


def test_coverage_json_overrides_the_gate_include_and_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping --include would narrow the report to the gated modules;
    dropping --fail-under=0 would make a report exit on the gate's threshold."""
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        Path(argv[argv.index("-o") + 1]).write_text('{"files": {}}', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(branch_coverage.subprocess, "run", fake_run)
    assert branch_coverage.coverage_json() == {"files": {}}
    assert len(seen) == 1
    assert "--include=src/dsl41/*" in seen[0]
    assert "--fail-under=0" in seen[0]
    assert seen[0][1:4] == ["-m", "coverage", "json"]


def test_coverage_json_failure_names_the_missing_steps(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "No data to report.")

    monkeypatch.setattr(branch_coverage.subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="coverage combine"):
        branch_coverage.coverage_json()
    assert "No data to report." in capsys.readouterr().err
