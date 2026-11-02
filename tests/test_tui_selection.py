"""Synthetic presentation filtering, clear controls and refresh behavior."""

from dataclasses import replace

import pytest

from dsl41.ir import BoxLinkage, JobIR
from dsl41.job_selection import EffectiveLabel, EffectiveLabels, JobSelectors
from dsl41.tui_selection import TuiFilters, TuiJobState, filter_jobs, filter_rows, matches_row


def catalog():
    return {
        "root": JobIR(name="root", job_type="BOX", passthrough={"application": "A", "group": "G"}),
        "task_one": JobIR(name="task_one", job_type="CMD", box=BoxLinkage(box_name="root")),
        "task_two": JobIR(
            name="task_two",
            job_type="CMD",
            box=BoxLinkage(box_name="root"),
            passthrough={"group": "H"},
        ),
        "plain": JobIR(name="plain", job_type="CMD"),
    }


def test_labels_name_and_view_are_anded_and_results_are_flat() -> None:
    filters = TuiFilters(JobSelectors(application="A", group="G"), "TASK one", "active")
    result = filter_jobs(
        catalog(),
        {
            "task_one": TuiJobState("RUNNING"),
            "task_two": TuiJobState("RUNNING"),
            "ghost": TuiJobState("RUNNING"),
        },
        filters,
        selected_job="task_one",
    )
    assert [row.name for row in result.rows] == ["task_one"]
    assert result.rows[0].labels.application == EffectiveLabel("A", "root")
    assert (result.matching_count, result.total_count) == (1, 4)
    assert result.selected_job == "task_one"
    assert result.filters.active_filters == (
        ("application", "A"),
        ("group", "G"),
        ("name", "TASK one"),
        ("view", "active"),
    )


def test_filter_refresh_preserves_controls_and_clears_disappearing_selection() -> None:
    jobs = catalog()
    filters = TuiFilters(JobSelectors(group="G"), "task")
    first = filter_jobs(jobs, {}, filters, selected_job="task_one")
    assert first.selected_job == "task_one"
    jobs["task_one"].passthrough["group"] = "H"
    second = filter_jobs(jobs, {}, first.filters, selected_job=first.selected_job)
    assert second.filters is filters
    assert second.matching_count == 0
    assert second.selected_job is None
    del jobs["task_two"]
    third = filter_jobs(jobs, {}, filters, selected_job="task_two")
    assert third.selected_job is None
    assert third.total_count == 3


def test_each_control_and_all_filters_can_be_cleared() -> None:
    filters = TuiFilters(JobSelectors("A", "G"), "task", "problems")
    assert filters.clear("application") == replace(filters, selectors=JobSelectors(group="G"))
    assert filters.clear("group") == replace(filters, selectors=JobSelectors(application="A"))
    assert filters.clear("name").name_filter == ""
    assert filters.clear("view").view_mode == "all"
    assert filters.clear() == TuiFilters()
    assert filters.clear().active_filters == ()
    assert filters.selectors == JobSelectors("A", "G")


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("all", ["plain", "root", "task_one", "task_two"]),
        ("problems", ["plain", "task_one", "task_two"]),
        ("active", ["task_one"]),
    ],
)
def test_existing_view_semantics_and_lexical_rows(mode, expected) -> None:
    states = {
        "task_one": TuiJobState("QUE_WAIT"),
        "task_two": TuiJobState("TERMINATED"),
        "plain": TuiJobState("SUCCESS", has_alarms=True),
    }
    assert [
        row.name for row in filter_jobs(catalog(), states, TuiFilters(view_mode=mode)).rows
    ] == expected


def test_case_empty_selector_and_catalog_only_counts() -> None:
    jobs = catalog()
    result = filter_jobs(jobs, {"ghost": TuiJobState()}, TuiFilters(JobSelectors(application="")))
    assert [row.name for row in result.rows] == ["plain"]
    assert result.total_count == 4
    assert filter_jobs(jobs, {}, TuiFilters(JobSelectors(application="a"))).matching_count == 0
    assert jobs["root"].passthrough == {"application": "A", "group": "G"}


def test_invalid_view_and_clear_control_refuse() -> None:
    with pytest.raises(ValueError, match="view"):
        TuiFilters(view_mode="unknown")
    with pytest.raises(ValueError, match="control"):
        TuiFilters().clear("unknown")


@pytest.mark.parametrize(
    "filters",
    [
        TuiFilters(view_mode="problems"),
        TuiFilters(name_filter="GHOST"),
        TuiFilters(name_filter="ghost", view_mode="problems"),
    ],
)
def test_status_ghost_survives_name_and_problem_filters_without_label_selectors(filters) -> None:
    states = {"ghost_failure": TuiJobState("FAILURE"), "catalog_job": TuiJobState("RUNNING")}
    labels = {
        "ghost_failure": None,
        "catalog_job": EffectiveLabels(EffectiveLabel("A", "catalog_job")),
    }
    result = filter_rows(states, labels, filters, selected_job="ghost_failure")
    assert [row.name for row in result.rows] == ["ghost_failure"]
    assert result.rows[0].labels is None
    assert result.matching_count == 1
    assert result.total_count == 2
    assert result.selected_job == "ghost_failure"
    assert result.filters is filters


@pytest.mark.parametrize(
    "selectors",
    [
        JobSelectors(application="A"),
        JobSelectors(group="G"),
        JobSelectors(application=""),
        JobSelectors(group=""),
    ],
)
def test_every_label_selector_excludes_ghost_status_rows(selectors) -> None:
    states = {"ghost": TuiJobState("FAILURE"), "catalog_job": TuiJobState("FAILURE")}
    labels = {"ghost": None, "catalog_job": EffectiveLabels()}
    filters = TuiFilters(selectors, view_mode="problems")
    result = filter_rows(states, labels, filters, selected_job="ghost")
    assert "ghost" not in [row.name for row in result.rows]
    assert result.selected_job is None
    assert result.total_count == 2
    assert not matches_row("ghost", states["ghost"], None, filters)


def test_status_row_labels_clear_and_refresh_without_a_catalog_read() -> None:
    states = {"job": TuiJobState("RUNNING"), "ghost": TuiJobState("RUNNING")}
    labels = {"job": EffectiveLabels(EffectiveLabel("A", "box")), "ghost": None}
    filters = TuiFilters(JobSelectors(application="A"))
    first = filter_rows(states, labels, filters, selected_job="job")
    assert [row.name for row in first.rows] == ["job"]
    labels["job"] = EffectiveLabels(EffectiveLabel("B", "box"))
    second = filter_rows(states, labels, first.filters, selected_job=first.selected_job)
    assert second.matching_count == 0
    assert second.selected_job is None
    cleared = filter_rows(states, labels, second.filters.clear("application"))
    assert [row.name for row in cleared.rows] == ["ghost", "job"]
    del states["ghost"]
    assert filter_rows(states, labels, cleared.filters, selected_job="ghost").selected_job is None
    with pytest.raises(ValueError, match="every status row"):
        filter_rows(states, {}, filters)
