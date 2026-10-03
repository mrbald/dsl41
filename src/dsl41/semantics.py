"""Semantic switches: the closed registry of interpretation choices an
operator may flip without a code change (runner-design ss8a, DL-252).

Each switch names one place where documented AutoSys behavior and dsl41's
own choice can differ, or where the documentation is unclear. The default
lives here, in code. A period records only explicit overrides, in its
runtime profile, so replay and audit read the same values the engine ran.
Each switch also names the jobs and the calendars it can change, so a
boundary that flips it classifies those jobs and the jobs that name those
calendars (period-model ss10.2).

Changing a default is a state-machine change and bumps
`STATE_MACHINE_VERSION`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from dsl41.capacity import checks_load, resource_type
from dsl41.conditions import ExitCodeAtom, StatusAtom, iter_atoms
from dsl41.ir import FwSpec

if TYPE_CHECKING:
    from dsl41.ir import CalendarIR, CatalogIR, JobIR


def _no_calendar(calendar: CalendarIR) -> bool:
    """A switch that changes no calendar's days."""
    return False


@dataclass(frozen=True)
class Switch:
    """One registry entry."""

    #: kebab-case, as `--semantics NAME=VALUE` spells it
    name: str
    #: the allowed values, in the order the help and the docs list them
    values: tuple[str, ...]
    #: the value used when the profile carries no override
    default: str
    #: the value that matches documented AutoSys behavior, or "unknown"
    autosys: str
    #: one line, plain English
    description: str
    #: whether flipping this switch can change how `job` runs or starts in
    #: `catalog` -- the classifier's edge from a job to this switch
    #: (period-model ss10.2)
    affects: Callable[[JobIR, CatalogIR], bool]
    #: whether flipping this switch can change the days `calendar` generates
    #: -- the classifier's edge from a calendar to this switch. A job reaches
    #: such a switch through the calendars it names.
    affects_calendar: Callable[[CalendarIR], bool] = _no_calendar


def _no_job(job: JobIR, _catalog: CatalogIR) -> bool:
    """A switch that reaches jobs only through their calendars."""
    return False


def _reads_wekr(calendar: CalendarIR) -> bool:
    """An extended calendar with a WEKR token in its rules: what
    `wekr-first-week` reads (SEM-37)."""
    from dsl41.autocal import reads_family

    return calendar.kind == "extended" and reads_family(calendar, "wekr")


def _has_lookback_atom(job: JobIR, _catalog: CatalogIR) -> bool:
    """A status or exit-code atom with a lookback qualifier in the job's
    condition, box_success or box_failure: what `ice-lookback` reads."""
    return any(
        isinstance(atom, (StatusAtom, ExitCodeAtom)) and atom.lookback is not None
        for _kind, cond, _span in job.iter_conditions()
        for atom in iter_atoms(cond)
    )


def _renewable_without_free(job: JobIR, catalog: CatalogIR) -> bool:
    """A `resources:` request with no FREE on a renewable resource: what
    `renewable-free` reads (DL-256). An absent res_type reads as renewable,
    as `capacity.release_policy` reads it."""
    return any(
        ref.free is None and resource_type(catalog.resources.get(ref.name)) in ("", "R")
        for ref in job.resources
    )


def _can_queue(job: JobIR, _catalog: CatalogIR) -> bool:
    """A job that can wait in QUE_WAIT: one that names a resource, or one
    whose positive priority makes its start check machine load (DL-247).
    What `queued-recheck` reads when such a job leaves the queue."""
    return bool(job.resources) or checks_load(job)


def fw_no_min_size(job: JobIR) -> bool:
    """Whether `job` is an FW job with no `watch_file_min_size`: what
    `fw-existence` can change (DL-258). `None` and `0` read the same -- the
    adapter's own `spec_ir.watch_file_min_size or 0` (runner_adapters.py) --
    so a job that spells `watch_file_min_size: 0` is folded in here too. A
    job with a minimum size is unaffected by the switch either way."""
    return isinstance(job.exec_, FwSpec) and not job.exec_.watch_file_min_size


def fw_existence_immediate(job: JobIR, existence: str) -> bool:
    """Whether `fw-existence=immediate` decides `job`'s FW completion: the
    switch reads `immediate` and `job` sets no `watch_file_min_size`. A job
    with a minimum size always needs the steady-size rule, whatever the
    switch says (runner_adapters.FileWatcherAdapter, runner_startup._resume_watch)."""
    return existence == "immediate" and fw_no_min_size(job)


def _fw_without_min_size(job: JobIR, _catalog: CatalogIR) -> bool:
    """`fw_no_min_size` in the registry's `affects` shape (DL-256)."""
    return fw_no_min_size(job)


#: The registry. Closed: a name that is not here is refused wherever it is
#: met, at profile construction and on the command line.
REGISTRY: Final[Mapping[str, Switch]] = MappingProxyType(
    {
        switch.name: switch
        for switch in (
            Switch(
                name="ice-lookback",
                values=("true", "ordinary"),
                default="true",
                autosys="true",
                description="what a condition atom with a lookback qualifier reads when its"
                " predecessor is on ice: true, or the ordinary on-ice table",
                affects=_has_lookback_atom,
            ),
            Switch(
                name="renewable-free",
                values=("Y", "A"),
                default="Y",
                autosys="Y",
                description="what a renewable resource request with no FREE does with its"
                " units: Y frees them only on SUCCESS and holds them after FAILURE or"
                " TERMINATED until RELEASE_RESOURCE, A frees them on every completion",
                affects=_renewable_without_free,
            ),
            Switch(
                name="queued-recheck",
                values=("0", "1", "2"),
                default="0",
                autosys="1",
                description="what a job leaving QUE_WAIT re-checks before it starts, as the"
                " vendor's EvaluateQueuedJobStarts: 0 nothing; 1 its condition, run_window and"
                " exclude_calendar; 2 also whether today is a run day",
                affects=_can_queue,
            ),
            Switch(
                name="fw-existence",
                values=("stable", "immediate"),
                default="stable",
                autosys="immediate",
                description="what an FW job with no watch_file_min_size does when the watched"
                " file exists: wait for the size to stay steady across polls like a job with a"
                " minimum size (stable, dsl41's own choice -- a file still being written is not"
                " complete), or complete at once, watch_interval ignored (immediate, the vendor"
                " reading)",
                affects=_fw_without_min_size,
            ),
            Switch(
                name="wekr-first-week",
                values=("first-full", "partial"),
                default="first-full",
                autosys="unknown",
                description="where week 1 of a WEKR token's year starts: on the first anchor"
                " day on or after January 1, or on January 1 itself",
                affects=_no_job,
                affects_calendar=_reads_wekr,
            ),
        )
    }
)

#: The switches that can change a calendar's days, so the ones a scheduler
#: compiles its extended calendars under (DL-259).
CALENDAR_SWITCHES: Final[tuple[str, ...]] = tuple(
    name for name, switch in REGISTRY.items() if switch.affects_calendar is not _no_calendar
)

IceLookback = Literal["true", "ordinary"]
RenewableFree = Literal["Y", "A"]
QueuedRecheck = Literal["0", "1", "2"]
FwExistence = Literal["stable", "immediate"]
WekrFirstWeek = Literal["first-full", "partial"]


@dataclass(frozen=True)
class SemanticSwitches:
    """The effective value of every switch: overrides over the registry
    defaults. One attribute per registry entry, named by the switch name
    with `-` read as `_`. No attribute has a default of its own: `resolve`
    is the one place a default is read, from the registry."""

    ice_lookback: IceLookback
    renewable_free: RenewableFree
    queued_recheck: QueuedRecheck
    fw_existence: FwExistence
    wekr_first_week: WekrFirstWeek

    def value(self, name: str) -> str:
        """The effective value of the switch called `name`."""
        return cast("str", getattr(self, _attribute(name)))


def _attribute(name: str) -> str:
    return name.replace("-", "_")


def check_overrides(overrides: Mapping[str, object]) -> dict[str, str]:
    """`overrides` as a plain dict, or ValueError naming what is wrong.

    An unknown name is refused with the list of known names, and a value
    outside a switch's set with the list of its values; the first bad entry
    refuses the whole mapping. An entry naming the switch's default is
    normalized away: it is the same reading as no override, and two
    spellings of one reading must be one profile and one hash (PR-15a)."""
    checked: dict[str, str] = {}
    for name, value in overrides.items():
        switch = REGISTRY.get(name)
        if switch is None:
            known = ", ".join(sorted(REGISTRY))
            raise ValueError(f"unknown semantic switch {name!r}; known switches: {known}")
        if not isinstance(value, str) or value not in switch.values:
            allowed = ", ".join(switch.values)
            raise ValueError(f"semantic switch {name!r}: {value!r} is not one of {allowed}")
        if value != switch.default:
            checked[name] = value
    return checked


def resolve(overrides: Mapping[str, str] | None = None) -> SemanticSwitches:
    """The effective switches for `overrides` (None or empty: the defaults)."""
    given = check_overrides(overrides or {})
    values = {
        _attribute(name): given.get(name, switch.default) for name, switch in REGISTRY.items()
    }
    return SemanticSwitches(**cast("dict[str, Any]", values))


#: The registry defaults, resolved once.
DEFAULTS: Final[SemanticSwitches] = resolve()


def parse_assignments(items: Iterable[str]) -> dict[str, str]:
    """`NAME=VALUE` words, as the CLI takes them, into checked overrides.

    A word without `=` is refused, and so is one name given two different
    values: picking either would drop the other silently."""
    parsed: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        name, value = name.strip(), value.strip()
        if not sep:
            raise ValueError(f"{item!r}: expected NAME=VALUE")
        if parsed.get(name, value) != value:
            raise ValueError(f"{name!r} is given twice, as {parsed[name]!r} and {value!r}")
        parsed[name] = value
    return check_overrides(parsed)


def help_text() -> str:
    """The registry as one plain-English help sentence per switch, so the
    CLI help is generated from the registry and cannot drift from it."""
    return " ".join(
        f"{switch.name}: {', '.join(switch.values)}; default {switch.default}."
        for switch in REGISTRY.values()
    )
