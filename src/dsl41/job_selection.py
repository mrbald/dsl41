"""Pure catalog selection for the AF helpers (docs/job-selection.md)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import cast

from pydantic import JsonValue

from dsl41.canon import is_scalar_string, is_wire_int
from dsl41.ir import JobIR, unquote_jil_value


def _object_fields(
    value: object, required: set[str], optional: frozenset[str] = frozenset()
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("selection value must be an object")
    if not required.issubset(value) or set(value).difference(required | optional):
        raise ValueError("selection object has missing or unknown fields")
    return cast(Mapping[str, object], value)


@dataclass(frozen=True, slots=True)
class EffectiveLabel:
    value: str | None = None
    origin: str | None = None

    def __post_init__(self) -> None:
        if (self.value is None) != (self.origin is None):
            raise ValueError("a label and its origin must both be present or absent")
        for value in (self.value, self.origin):
            if value is not None and (
                not isinstance(value, str) or not value or not is_scalar_string(value)
            ):
                raise ValueError("effective labels and origins must be non-empty scalar strings")

    def as_dict(self) -> dict[str, JsonValue]:
        return {"value": self.value, "origin": self.origin}

    @classmethod
    def from_dict(cls, value: object) -> EffectiveLabel:
        fields = _object_fields(value, {"value", "origin"})
        return cls(cast(str | None, fields["value"]), cast(str | None, fields["origin"]))


@dataclass(frozen=True, slots=True)
class EffectiveLabels:
    application: EffectiveLabel = EffectiveLabel()
    group: EffectiveLabel = EffectiveLabel()

    def as_dict(self) -> dict[str, JsonValue]:
        return {"application": self.application.as_dict(), "group": self.group.as_dict()}

    @classmethod
    def from_dict(cls, value: object) -> EffectiveLabels:
        fields = _object_fields(value, {"application", "group"})
        return cls(
            EffectiveLabel.from_dict(fields["application"]),
            EffectiveLabel.from_dict(fields["group"]),
        )


@dataclass(frozen=True, slots=True)
class JobSelectors:
    """None omits a selector; the empty string selects unlabelled jobs in queries."""

    application: str | None = None
    group: str | None = None

    def __post_init__(self) -> None:
        for value in (self.application, self.group):
            if value is not None and (not isinstance(value, str) or not is_scalar_string(value)):
                raise ValueError("selectors must be Unicode scalar strings or None")

    @property
    def active(self) -> bool:
        return self.application is not None or self.group is not None

    def matches(self, labels: EffectiveLabels) -> bool:
        return all(
            wanted is None or wanted == (effective.value or "")
            for wanted, effective in (
                (self.application, labels.application),
                (self.group, labels.group),
            )
        )

    def as_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (("application", self.application), ("group", self.group))
            if value is not None
        }

    @classmethod
    def from_dict(cls, value: object) -> JobSelectors:
        fields = _object_fields(value, set(), frozenset({"application", "group"}))
        return cls(
            cast(str | None, fields.get("application")),
            cast(str | None, fields.get("group")),
        )


def _catalog_paths(catalog: Mapping[str, JobIR]) -> dict[str, tuple[str, ...]]:
    paths: dict[str, tuple[str, ...]] = {}
    for name, job in catalog.items():
        if name != job.name:
            raise ValueError("catalog keys must equal job names")
        trail: list[str] = []
        visiting: set[str] = set()
        current: str | None = name
        while current is not None and current not in paths:
            if current in visiting:
                raise ValueError("catalog box ancestry contains a cycle")
            if current not in catalog:
                raise ValueError(f"catalog ancestor {current!r} is missing")
            visiting.add(current)
            trail.append(current)
            parent = catalog[current].box.box_name
            if parent is not None and parent in catalog and catalog[parent].job_type != "BOX":
                raise ValueError(f"catalog ancestor {parent!r} is not a box")
            current = parent
        path = () if current is None else paths[current]
        for member in reversed(trail):
            path = (*path, member)
            paths[member] = path
    return paths


def ordered_targets(catalog: Mapping[str, JobIR], targets: Iterable[str]) -> tuple[str, ...]:
    """Depth-first box order, with lexical siblings, including unselected ancestors in keys."""
    names = tuple(targets)
    if len(set(names)) != len(names):
        raise ValueError("duplicate targets")
    if unknown := set(names).difference(catalog):
        raise ValueError(f"targets outside the applied catalog: {sorted(unknown)}")
    paths = _catalog_paths(catalog)
    return tuple(sorted(names, key=paths.__getitem__))


def resolve_effective_labels(catalog: Mapping[str, JobIR]) -> dict[str, EffectiveLabels]:
    """Derive independent labels and their declaring jobs without changing declared JIL."""
    result: dict[str, EffectiveLabels] = {}
    for name in ordered_targets(catalog, catalog):
        job = catalog[name]
        inherited = result[job.box.box_name] if job.box.box_name is not None else EffectiveLabels()
        declared = {key.lower(): unquote_jil_value(value) for key, value in job.passthrough.items()}
        result[name] = EffectiveLabels(
            application=(
                EffectiveLabel(declared["application"], name)
                if declared.get("application")
                else inherited.application
            ),
            group=EffectiveLabel(declared["group"], name)
            if declared.get("group")
            else inherited.group,
        )
    return result


@dataclass(frozen=True, slots=True)
class SelectedJob:
    name: str
    box_name: str | None
    labels: EffectiveLabels
    state_rev: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or not is_scalar_string(self.name):
            raise ValueError("selected job names must be non-empty scalar strings")
        if self.box_name is not None and (
            not isinstance(self.box_name, str)
            or not self.box_name
            or not is_scalar_string(self.box_name)
        ):
            raise ValueError("selected job parents must be non-empty scalar strings or None")
        if not is_wire_int(self.state_rev) or self.state_rev < 0:
            raise ValueError("job revisions must be non-negative integers")

    def as_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "box_name": self.box_name,
            "labels": self.labels.as_dict(),
            "state_rev": self.state_rev,
        }

    @classmethod
    def from_dict(cls, value: object) -> SelectedJob:
        fields = _object_fields(value, {"name", "box_name", "labels", "state_rev"})
        return cls(
            cast(str, fields["name"]),
            cast(str | None, fields["box_name"]),
            EffectiveLabels.from_dict(fields["labels"]),
            cast(int, fields["state_rev"]),
        )


@dataclass(frozen=True, slots=True)
class SelectionSnapshot:
    baseline_id: str
    applied_index: int
    config_rev: int
    selectors: JobSelectors
    jobs: tuple[SelectedJob, ...]
    catalog_count: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.baseline_id, str)
            or not self.baseline_id
            or not is_scalar_string(self.baseline_id)
        ):
            raise ValueError("baseline_id must be a non-empty Unicode scalar string")
        for value in (self.applied_index, self.config_rev, self.catalog_count):
            if not is_wire_int(value) or value < 0:
                raise ValueError(
                    "snapshot indexes, revisions and counts must be non-negative integers"
                )
        if len(set(self.targets)) != len(self.jobs) or len(self.jobs) > self.catalog_count:
            raise ValueError("snapshot jobs must be unique catalog members")
        if any(not is_wire_int(job.state_rev) or job.state_rev < 0 for job in self.jobs):
            raise ValueError("job revisions must be non-negative integers")

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(job.name for job in self.jobs)

    def by_name(self) -> dict[str, SelectedJob]:
        """The name-keyed selection shape; transport serialization belongs to the caller."""
        return {job.name: job for job in self.jobs}

    def as_dict(self) -> dict[str, JsonValue]:
        """A detached JSON object retaining order, effective labels, origins and every pin."""
        return {
            "baseline_id": self.baseline_id,
            "applied_index": self.applied_index,
            "config_rev": self.config_rev,
            "selectors": dict(self.selectors.as_dict()),
            "jobs": [job.as_dict() for job in self.jobs],
            "catalog_count": self.catalog_count,
        }

    @classmethod
    def from_dict(cls, value: object) -> SelectionSnapshot:
        fields = _object_fields(
            value,
            {
                "baseline_id",
                "applied_index",
                "config_rev",
                "selectors",
                "jobs",
                "catalog_count",
            },
        )
        jobs = fields["jobs"]
        if not isinstance(jobs, list):
            raise ValueError("selection jobs must be an array")
        return cls(
            baseline_id=cast(str, fields["baseline_id"]),
            applied_index=cast(int, fields["applied_index"]),
            config_rev=cast(int, fields["config_rev"]),
            selectors=JobSelectors.from_dict(fields["selectors"]),
            jobs=tuple(SelectedJob.from_dict(job) for job in jobs),
            catalog_count=cast(int, fields["catalog_count"]),
        )


def selection_snapshot(
    catalog: Mapping[str, JobIR],
    selectors: JobSelectors,
    *,
    baseline_id: str,
    applied_index: int,
    config_rev: int,
    job_revisions: Mapping[str, int],
) -> SelectionSnapshot:
    """Consume values from one caller-owned coherent read; exclude store-only ghost rows."""
    labels = resolve_effective_labels(catalog)
    jobs: list[SelectedJob] = []
    for name in ordered_targets(catalog, catalog):
        if selectors.matches(labels[name]):
            if name not in job_revisions:
                raise ValueError(f"missing revision for catalog job {name!r}; supply 0 for no row")
            jobs.append(
                SelectedJob(name, catalog[name].box.box_name, labels[name], job_revisions[name])
            )
    return SelectionSnapshot(
        baseline_id, applied_index, config_rev, selectors, tuple(jobs), len(catalog)
    )
