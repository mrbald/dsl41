"""The generated job lifecycle view, scripts/render_job_lifecycle.py."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from dsl41.job_chart import JOB
from dsl41.state_machine import COMPLETION, Branch, Choice, Region, Row, State, Statechart

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "render_job_lifecycle.py"


def _load() -> ModuleType:
    """scripts/ is not a package, so the test loads the script by path."""
    spec = importlib.util.spec_from_file_location("render_job_lifecycle", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_job_lifecycle"] = module
    spec.loader.exec_module(module)
    return module


rjl = _load()


def test_the_committed_doc_is_the_rendering() -> None:
    """Breaks when the job chart changed and nobody ran
    `uv run python scripts/render_job_lifecycle.py`."""
    assert rjl.is_current(REPO_ROOT), (
        "docs/job-chart-lifecycle.md is stale; run `uv run python scripts/render_job_lifecycle.py`"
    )


def test_a_changed_declaration_makes_the_doc_stale() -> None:
    """Dropping a shown row, and changing a shown row's guard, each leave the committed
    file behind."""
    dropped = replace(JOB, rows=tuple(r for r in JOB.rows if r.id != "job.101"))
    assert not rjl.is_current(REPO_ROOT, dropped)
    guarded = replace(
        JOB, rows=tuple(replace(r, guard="armed") if r.id == "job.101" else r for r in JOB.rows)
    )
    assert not rjl.is_current(REPO_ROOT, guarded)


def test_the_shown_and_folded_rows_are_the_job_chart_rows() -> None:
    view = rjl.sort_rows(JOB)
    folded = sum(
        rjl._rows(edges) for edges in (view.ignored, view.unreachable, view.internal, view.selves)
    )
    assert view.shown_rows() + folded == len(JOB.rows)
    assert view.shown_rows() > 0 and folded > 0
    assert f"Rows in the chart: {len(JOB.rows)}." in rjl.render()


def test_the_job_chart_view_starts_where_the_pad_does() -> None:
    lines = rjl.render().splitlines()
    assert lines[lines.index("## Lifecycle") + 2 :][:3] == [
        "```text",
        "INACTIVE <- initial",
        "INACTIVE = lifecycle + STATUS [injects; injected_inactive]",
    ]


def test_the_lifecycle_blocks_follow_the_reading_order() -> None:
    lines = rjl.render().splitlines()
    start = lines.index("## Lifecycle") + 4
    body = lines[start : lines.index("```", start)]
    assert [line.split(" = ")[0] for line in body if " = " in line] == list(rjl.READING_ORDER)


def test_the_ways_into_a_target_are_sorted_by_source_then_event_then_guard() -> None:
    view = rjl.sort_rows(JOB)
    inactive = [e for e in view.shown["status"] if e.target == "INACTIVE"]
    order = ["lifecycle", "TERMINAL", "FAILED", "QUE_WAIT"]
    sources = list(dict.fromkeys(e.source for e in inactive))
    assert sources == order
    keys = [(order.index(e.source), e.event, e.guard) for e in inactive]
    assert keys == sorted(keys)


def test_no_row_id_reaches_the_file() -> None:
    assert "job." not in "".join(
        line for line in rjl.render(full=True).splitlines() if not line.startswith("This file")
    )


# ---- a small chart

DOOR = Statechart(
    "door",
    State(
        "door",
        regions=(
            Region(
                "status",
                "closed",
                (State("closed"), State("open"), State("ajar")),
            ),
            Region(
                "flags",
                "flags",
                (
                    State(
                        "flags",
                        regions=(
                            Region("lock", "lock_off", (State("lock_off"), State("lock_on"))),
                        ),
                    ),
                ),
            ),
            Region("holding", "none", (State("none"), State("has"))),
        ),
    ),
    (
        Row("door.01", "closed", "push", "open", note="a hard shove"),
        Row("door.02", "ajar", "push", "open", guard="strong", action="swing"),
        Row("door.03", "ajar", COMPLETION, "closed"),
        Row("door.04", "closed", "kick", "kicked", guard="loud"),
        Row("door.05", "open", "wait", kind="ignored"),
        Row("door.06", "open", "tick", kind="internal", action="swing"),
        Row("door.07", "closed", "pull", "open", note="see door.01"),
        Row("door.08", "open", "gone", kind="ignored", mark="unreachable", note="never posted"),
        Row("door.09", "lock_on", "lock", "lock_on"),
        Row("door.10", "lock_off", "lock", "lock_on", note="x" * 120),
        Row("door.11", "lock_on", "unlock", "lock_off"),
        Row("door.12", "none", "take", "has"),
        Row("door.13", "open", "slam", kind="refusal", code="LOUD"),
    ),
    choices=(
        Choice(
            "kicked",
            (Branch("door.21", "open", guard="wide"), Branch("door.22", "closed")),
        ),
    ),
    actions={"swing": lambda _ctx: None},
    clock="logical",
)


def test_the_initial_states_are_the_leaves_the_initials_reach() -> None:
    regions = rjl.view_regions(DOOR)
    assert list(regions) == ["status", "lock", "holding"]
    assert {name: rjl.initial_state(region) for name, region in regions.items()} == {
        "status": "closed",
        "lock": "lock_off",
        "holding": "none",
    }


def test_the_job_chart_initials_reach_inactive_and_the_off_states() -> None:
    regions = rjl.view_regions(JOB)
    assert {name: rjl.initial_state(region) for name, region in regions.items()} == {
        "status": "INACTIVE",
        "ice": "ice_off",
        "hold": "hold_off",
        "noexec": "noexec_off",
        "holding": "none",
    }


def test_rows_group_by_target_with_alternatives_under_the_equals_sign() -> None:
    """A target's `||` lines end under its `=`; a block follows the declaration order of
    the targets."""
    view = rjl.sort_rows(DOOR)
    assert rjl._blocks(view.shown["status"]) == [
        "closed = closed + kick [loud; else]",
        "      || ajar + (completion)",
        "open = closed + kick [loud; wide]",
        "    || closed + pull",
        "    || closed + push // a hard shove",
        "    || ajar + push [strong]",
    ]


def test_a_choice_row_makes_one_line_per_branch() -> None:
    view = rjl.sort_rows(DOOR)
    kicks = [e for e in view.shown["status"] if e.event == "kick"]
    assert [(e.target, e.guard) for e in kicks] == [
        ("closed", "loud; else"),
        ("open", "loud; wide"),
    ]
    assert rjl._rows(kicks) == 1


def test_a_note_is_dropped_when_it_names_a_row_id_or_does_not_fit() -> None:
    view = rjl.sort_rows(DOOR)
    text = "\n".join(rjl._blocks(view.shown["lock"]))
    assert text == "lock_off = lock_on + unlock\nlock_on = lock_off + lock"
    status = rjl._blocks(view.shown["status"])
    assert "    || closed + push // a hard shove" in status
    assert "    || closed + pull" in status


def test_a_choice_row_without_a_row_guard_prints_the_else_alone() -> None:
    bare = replace(
        DOOR, rows=tuple(replace(r, guard="") if r.id == "door.04" else r for r in DOOR.rows)
    )
    guards = [e.guard for e in rjl.sort_rows(bare).shown["status"] if e.event == "kick"]
    assert guards == ["else", "wide"]


def test_an_action_docstring_is_never_a_note() -> None:
    def swing(_ctx: object) -> None:
        """Swings the door open."""

    documented = replace(DOOR, actions={"swing": swing})
    assert "Swings" not in rjl.render(documented, full=True)


def test_folded_rows_are_counted_by_kind_and_sum_to_the_chart() -> None:
    view = rjl.sort_rows(DOOR)
    assert rjl._rows(view.ignored) == 1
    assert rjl._rows(view.unreachable) == 1
    assert rjl._rows(view.internal) == 2
    assert rjl._rows(view.selves) == 1
    assert view.shown_rows() == 8
    assert rjl.counts(DOOR)[0] == "- Ignored rows: 1. The event is consumed and nothing moves."
    assert rjl.counts(DOOR)[-1] == "Shown: 8. Folded: 5. Rows in the chart: 13."


def test_full_adds_the_folded_rows_and_the_default_does_not() -> None:
    plain = rjl.render(DOOR)
    full = rjl.render(DOOR, full=True)
    assert "## Ignored" not in plain and "## Ignored" in full
    assert full.startswith(plain.rstrip("\n"))
    assert "open + wait\n" in full
    assert "open + gone // never posted" in full
    assert "open + tick\n" in full and "open + slam // refusal LOUD" in full
    assert "lock_on = lock_on + lock" in full


def test_a_chart_without_the_three_regions_is_refused() -> None:
    bare = Statechart("bare", State("bare"), (), clock="logical")
    with pytest.raises(ValueError, match="needs the regions"):
        rjl.render(bare)


def test_a_row_across_regions_is_refused_not_dropped() -> None:
    crossing = replace(DOOR, rows=(*DOOR.rows, Row("door.30", "closed", "bang", "lock_on")))
    with pytest.raises(ValueError, match="door.30"):
        rjl.sort_rows(crossing)


def test_a_matching_doc_is_current(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "job-chart-lifecycle.md").write_text(rjl.render(DOOR), encoding="utf-8")
    assert rjl.is_current(tmp_path, DOOR)
    assert not rjl.is_current(tmp_path, JOB)


def test_a_missing_doc_is_not_current(tmp_path: Path) -> None:
    assert not rjl.is_current(tmp_path, DOOR)
