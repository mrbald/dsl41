"""Presentation-only AF filters with no widget, transport or engine dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from dsl41.ir import JobIR
from dsl41.job_selection import EffectiveLabels, JobSelectors, resolve_effective_labels

ViewMode = Literal["all", "problems", "active"]
FilterControl = Literal["name", "application", "group", "view", "all"]


@dataclass(frozen=True, slots=True)
class TuiFilters:
    selectors: JobSelectors = JobSelectors()
    name_filter: str = ""
    view_mode: ViewMode = "all"

    def __post_init__(self) -> None:
        if self.view_mode not in ("all", "problems", "active"):
            raise ValueError("unknown TUI view mode")

    @property
    def active_filters(self) -> tuple[tuple[str, str], ...]:
        filters = list(self.selectors.as_dict().items())
        if self.name_filter:
            filters.append(("name", self.name_filter))
        if self.view_mode != "all":
            filters.append(("view", self.view_mode))
        return tuple(filters)

    def clear(self, control: FilterControl = "all") -> TuiFilters:
        if control == "all":
            return TuiFilters()
        if control == "name":
            return replace(self, name_filter="")
        if control == "view":
            return replace(self, view_mode="all")
        if control == "application":
            return replace(self, selectors=replace(self.selectors, application=None))
        if control == "group":
            return replace(self, selectors=replace(self.selectors, group=None))
        raise ValueError("unknown filter control")


@dataclass(frozen=True, slots=True)
class TuiJobState:
    status: str = "INACTIVE"
    has_alarms: bool = False


@dataclass(frozen=True, slots=True)
class TuiRow:
    name: str
    labels: EffectiveLabels | None


@dataclass(frozen=True, slots=True)
class TuiSelection:
    filters: TuiFilters
    rows: tuple[TuiRow, ...]
    total_count: int
    selected_job: str | None

    @property
    def matching_count(self) -> int:
        return len(self.rows)


def matches_row(
    name: str, state: TuiJobState, labels: EffectiveLabels | None, filters: TuiFilters
) -> bool:
    """Match one coherent status row; None marks a store-only ghost."""
    if labels is None:
        if filters.selectors.active:
            return False
    elif not filters.selectors.matches(labels):
        return False
    if filters.view_mode == "active" and state.status not in ("STARTING", "RUNNING", "QUE_WAIT"):
        return False
    if filters.view_mode == "problems" and not (
        state.status in ("FAILURE", "TERMINATED", "QUE_WAIT") or state.has_alarms
    ):
        return False
    return all(term in name.lower() for term in filters.name_filter.lower().split())


def filter_rows(
    states: Mapping[str, TuiJobState],
    effective_labels: Mapping[str, EffectiveLabels | None],
    filters: TuiFilters,
    *,
    selected_job: str | None = None,
) -> TuiSelection:
    """Filter status rows using their precomputed labels, without a second catalog read."""
    if set(states).difference(effective_labels):
        raise ValueError("every status row needs effective labels or explicit None for a ghost")
    rows = tuple(
        TuiRow(name, effective_labels[name])
        for name in sorted(states)
        if matches_row(name, states[name], effective_labels[name], filters)
    )
    visible = {row.name for row in rows}
    return TuiSelection(
        filters, rows, len(states), selected_job if selected_job in visible else None
    )


def filter_jobs(
    catalog: Mapping[str, JobIR],
    states: Mapping[str, TuiJobState],
    filters: TuiFilters,
    *,
    selected_job: str | None = None,
) -> TuiSelection:
    """Catalog-only convenience entry; live TUI callers use filter_rows or matches_row."""
    labels = resolve_effective_labels(catalog)
    return filter_rows(
        {name: states.get(name, TuiJobState()) for name in catalog},
        labels,
        filters,
        selected_job=selected_job,
    )
