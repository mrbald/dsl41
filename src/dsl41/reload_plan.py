"""Diff a prepared main-config estate against the live catalog and store a plan (DL-486).

This is spec-reload steps 1 to 6, without a CLI verb and without any access to the engine. The
engine's part is the `Live` record: the catalog it runs, the estate settings it runs under, the
catalog revision and each row's status. `reread` changes nothing live. It writes one directory,
`<plans_dir>/<token>/`, and a second call with the same inputs rewrites the same directory and
returns the same token.

`reread` takes a prepared main-config candidate (DL-486). Its captured inputs and
settings bind the token and the stored plan. `main_config.prepare` owns source loading,
substitution, lowering and lint policy. `Estate.settings` holds the estate-wide values
compared by `reload_diff.diff`.

The engine builds `Live.settings` with `estate_settings`. `job_env` is retained in these
settings and the token-bound runtime revision but excluded from job-definition hashes
(DL-447). An environment-only update stops nothing and may apply while boxes run.

Refusals, all as `RereadRefused`: a blocking lint finding, changed host launch settings,
or a preflight ERROR. Preflight WARNs go into `plan.json` as `preflight_warnings`.
Running boxes are restarted by update (DL-463).

Write order in `plans/<token>/`: each candidate file by the durable-write sequence, `plan.json`
last. `plan.json` lists every part with its sha256, so `read_plan` takes a directory as complete
only when `plan.json` is present, every listed file is there with those bytes, and the token
recomputed from the parts and the live revision is the directory's name.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from dsl41.ast_jil import JilFile, render_statement
from dsl41.canon import ARTIFACT_FORMAT_VERSION, CanonError, canonical_bytes, decode
from dsl41.ir import CatalogIR
from dsl41.reload_diff import Plan, catalog_hash_v2, diff, plan_token, running_boxes
from dsl41.runner_preflight import PreflightItem, preflight
from dsl41.runner_config import MachinePolicy
from dsl41.runtime_policy import policy_values
from dsl41.runner_procid import durable_write, mkdir_durable
from dsl41.semantics import check_overrides
from dsl41.semantics import resolve as resolve_switches
from dsl41.timezones import alias_table

#: The base zone of an estate that names none: an engine launched with no `--timezone` runs UTC.
DEFAULT_TZ = "UTC"

#: Statuses of a job with a process the plan would have to stop (spec-reload `update`).
STOPPABLE = frozenset({"RUNNING", "STARTING"})

PLAN_JSON = "plan.json"


class RereadRefused(Exception):
    """The estate cannot become a plan: one sentence per finding, for an exit 2."""

    def __init__(self, findings: Sequence[str]) -> None:
        super().__init__("; ".join(findings))
        self.findings = tuple(findings)


class RetiredPlan(RereadRefused):
    """A reload-plan dialect retired under DL-486."""


@dataclass(frozen=True)
class Live:
    """What the engine supplies: the running catalog, its settings, its revision, the statuses,
    the flags each tombstone of a removed job kept, by name (spec-reload: re-adding a removed
    name shows them), and the run numbers."""

    catalog: CatalogIR
    settings: Mapping[str, object]
    revision: int
    status: Mapping[str, str]
    tombstones: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: each row's current run number, beside its status: the run a stop interrupts
    runs: Mapping[str, int] = field(default_factory=dict)
    launch: Mapping[str, object] = field(default_factory=dict)
    path_environment: Mapping[str, str] | None = None


@dataclass(frozen=True)
class Estate:
    """An estate as read from disk: the lowered catalog, its settings and its named bytes."""

    catalog: CatalogIR
    settings: Mapping[str, object]
    parts: tuple[tuple[str, bytes], ...]
    environment: str | None = None
    properties: tuple[str, ...] = ()
    #: the parsed JIL files, after substitution, in catalog order (`spec_texts`)
    parsed: tuple[JilFile, ...] = field(default=(), repr=False)
    #: the estate directory, absolute
    directory: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RereadResult:
    token: str
    plan: Plan
    stop: tuple[str, ...]
    plan_dir: Path
    plan_json: Path
    files: tuple[Path, ...]
    #: each added name a tombstone kept flags for, with the flags (`Live.tombstones`)
    restored: tuple[tuple[str, tuple[str, ...]], ...] = ()


def estate_settings(
    timezone: str | None,
    tz_aliases: Mapping[str, str] | None,
    semantics: Mapping[str, str],
    catalog: CatalogIR,
    *,
    job_env: Mapping[str, str] | None = None,
    runtime_policy: Mapping[str, object] | None = None,
    fw_default_interval_s: int = 60,
) -> dict[str, object]:
    """The estate-wide settings `reload_diff.diff` compares: timezone, timezone_aliases,
    semantics, job_env, external_instances, the keys `runtime_policy.policy_values` returns,
    and fw_default_interval_s when nondefault. Values use the engine's spelling so an
    unchanged candidate compares equal."""
    return {
        "timezone": timezone or DEFAULT_TZ,
        "timezone_aliases": alias_table(tz_aliases),
        "semantics": check_overrides(semantics),
        "job_env": dict(job_env or {}),
        **policy_values(runtime_policy or {}),
        **({"fw_default_interval_s": fw_default_interval_s} if fw_default_interval_s != 60 else {}),
        "external_instances": {
            name: xinst.model_dump(mode="json", exclude={"span"})
            for name, xinst in catalog.external_instances.items()
        },
    }


def preflight_items(
    estate: Estate,
    start: datetime | None,
    startup_env: Mapping[str, str] | None = None,
) -> list[PreflightItem]:
    """Check the candidate and its captured launch settings (DL-437, DL-486)."""
    base_tz = estate.settings["timezone"]
    aliases = estate.settings["timezone_aliases"]
    semantics = estate.settings["semantics"]
    assert base_tz is None or isinstance(base_tz, str)
    assert aliases is None or isinstance(aliases, dict)
    assert isinstance(semantics, dict)
    return preflight(
        estate.catalog,
        execution=True,
        start=start,
        default_tz=base_tz,
        tz_aliases=aliases,
        timezone_map_source="estate.timezone_map",
        semantics=resolve_switches(semantics),
        job_env=cast(dict[str, str], estate.settings.get("job_env", {})),
        startup_env=startup_env,
        set_job_attribute_environmentals=cast(
            bool, estate.settings.get("set_job_attribute_environmentals", True)
        ),
        job_profile=cast(str | None, estate.settings.get("job_profile")),
        job_shell=cast(str | None, estate.settings.get("job_shell")),
        std_file_mode=cast(Any, estate.settings.get("std_file_mode", "append")),
        hook_configured=bool(estate.metadata["launch"]["status_hook"]),
        machine_policy=cast(MachinePolicy, estate.metadata["launch"]["machine_policy"]),
        as_machine=frozenset(estate.metadata["launch"]["as_machine"]),
        known_unsupported=(),
    )


def _plan_document(
    token: str,
    live: Live,
    plan: Plan,
    stop: Sequence[str],
    estate: Estate,
    warnings: Sequence[PreflightItem],
    restored: Sequence[tuple[str, tuple[str, ...]]],
) -> dict[str, Any]:
    document = {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "token": token,
        "live_rev": live.revision,
        "live_digest": catalog_hash_v2(live.catalog),
        "added": list(plan.added),
        "removed": list(plan.removed),
        "changed": [
            {
                "name": change.name,
                "old_hash": change.old_hash,
                "new_hash": change.new_hash,
                "reason": change.reason,
            }
            for change in plan.changed
        ],
        "unchanged": list(plan.unchanged),
        "stop": list(stop),
        "preflight_warnings": [
            {"code": item.code, "job": item.job, "message": item.message} for item in warnings
        ],
        "settings": dict(estate.settings),
        "estate_dir": estate.directory,
        "environment": estate.environment,
        "properties": list(estate.properties),
        "restored": [{"name": name, "flags": list(flags)} for name, flags in restored],
        "parts": [
            {"name": name, "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in estate.parts
        ],
    }
    from dsl41.main_config import policy_report

    policy, report = policy_report(estate)
    document.update(
        plan_format_version=2,
        lint_policy=policy.model_dump(mode="json"),
        lint_findings=[v.model_dump(mode="json") for v in report.violations],
        source_mapping=estate.metadata["records"],
    )
    return document


def spec_texts(parsed: Sequence[JilFile], catalog: CatalogIR) -> dict[str, str]:
    """job -> its preserve-rendered statements, in file order (the control socket's `spec`
    verb, DL-64): every statement whose subject is a catalog job, `insert_job` and any later
    `update_job` or `delete_job`."""
    texts: dict[str, str] = {}
    for jil_file in parsed:
        for stmt in jil_file.statements:
            if stmt.subject in catalog.jobs:
                texts[stmt.subject] = texts.get(stmt.subject, "") + render_statement(stmt)
    return texts


def box_members(catalog: CatalogIR, boxes: Sequence[str]) -> set[str]:
    """All descendants of the named boxes (DL-463)."""
    members = set(boxes)
    while True:
        more = {name for name, job in catalog.jobs.items() if job.box.box_name in members}
        if more <= members:
            return members - set(boxes)
        members |= more


def restart_boxes(plan: Plan, live: Live, candidate: CatalogIR) -> tuple[str, ...]:
    """Only outermost structurally edited running boxes need a forced start (DL-463)."""
    boxes = set(running_boxes(plan, candidate, live.status))
    nested = set().union(*(box_members(live.catalog, (box,)) for box in boxes))
    return tuple(sorted(boxes - nested))


def stop_set(plan: Plan, live: Live, candidate: CatalogIR | None = None) -> tuple[str, ...]:
    """Changed or removed jobs with a process that is up, sorted. A box has none (spec-reload
    step 5). `reread` previews it and the update's admission finds it again."""
    names = {change.name for change in plan.changed} | set(plan.removed)
    if candidate is not None:
        roots = restart_boxes(plan, live, candidate)
        names |= box_members(live.catalog, roots) | box_members(candidate, roots)
    return tuple(
        sorted(
            name
            for name in names
            if name in live.catalog.jobs
            and live.status.get(name) in STOPPABLE
            and live.catalog.jobs[name].job_type != "BOX"
        )
    )


def check_reload_candidate(
    estate: Estate, live: Live, start: datetime | None = None
) -> list[PreflightItem]:
    """Apply the preparation refusals to preview and update alike (DL-482)."""
    from dsl41.main_config import policy_report

    policy, report = policy_report(estate)
    findings = [v.render() for v in report.violations if policy.blocks(v.severity)]
    if live.launch != estate.metadata["launch"]:
        changed = sorted(
            key
            for key in estate.metadata["launch"]
            if live.launch.get(key) != estate.metadata["launch"][key]
        )
        raise RereadRefused(
            [
                "engine host launch settings changed: "
                + ", ".join(changed)
                + "; restart the engine to apply them before update"
            ]
        )
    if findings:
        raise RereadRefused(findings)
    items = preflight_items(estate, start, live.path_environment)
    errors = [item for item in items if item.severity == "ERROR"]
    if errors:
        raise RereadRefused(
            [
                f"preflight ERROR [{item.code}]{f' {item.job}' if item.job else ''}: {item.message}"
                for item in errors
            ]
        )

    return items


def reread(
    live: Live,
    plans_dir: Path,
    *,
    start: datetime | None = None,
    candidate: Estate,
) -> RereadResult:
    """Spec-reload steps 1 to 6: refuse, or store the plan and return it."""
    estate = candidate
    items = check_reload_candidate(estate, live, start)
    plan = diff(live.catalog, estate.catalog, live.settings, estate.settings)
    stop = stop_set(plan, live, estate.catalog)
    restored = tuple(
        (name, tuple(live.tombstones[name])) for name in plan.added if live.tombstones.get(name)
    )
    token = plan_token(estate.parts, live.revision)
    plan_dir = plans_dir / token
    mkdir_durable(str(plan_dir))
    files: list[Path] = []
    for name, data in estate.parts:
        target = plan_dir / name
        mkdir_durable(str(target.parent))
        durable_write(str(target), data)
        files.append(target)
    plan_json = plan_dir / PLAN_JSON
    document = _plan_document(
        token, live, plan, stop, estate, [i for i in items if i.severity == "WARN"], restored
    )
    durable_write(str(plan_json), canonical_bytes(document) + b"\n")
    return RereadResult(token, plan, stop, plan_dir, plan_json, tuple(files), restored)


def read_plan(plans_dir: Path, token: str) -> dict[str, Any] | None:
    """The stored `plan.json` as a dict, or None when the directory is not a complete plan.

    Complete means `plan.json` decodes, names this token, every part it lists is a file in the
    directory with the listed sha256, and the token recomputed from those bytes and the
    recorded live revision is this token. A crash before `plan.json` leaves no plan.
    Raises RereadRefused for a retired or unsupported plan version (DL-486)."""
    plan_dir = plans_dir / token
    try:
        document = decode((plan_dir / PLAN_JSON).read_bytes())
        if not isinstance(document, dict) or document.get("token") != token:
            return None
        version = document.get("plan_format_version", 1)
        if type(version) is int and version == 1:
            raise RetiredPlan(["reload plan version 1 is retired (DL-486)"])
        if type(version) is not int or version != 2:
            raise RereadRefused([f"unsupported plan_format_version {version!r}"])
        parts: list[tuple[str, bytes]] = []
        for part in document["parts"]:
            name = part["name"]
            if (
                not isinstance(name, str)
                or Path(name).is_absolute()
                or any(p in ("", ".", "..") for p in name.split("/"))
            ):
                return None
            data = (plan_dir / part["name"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != part["sha256"]:
                return None
            parts.append((part["name"], data))
        if plan_token(parts, document["live_rev"]) != token:
            return None
    except (OSError, CanonError, KeyError, TypeError):
        return None
    return document


__all__ = [
    "PLAN_JSON",
    "STOPPABLE",
    "Estate",
    "Live",
    "RereadRefused",
    "RereadResult",
    "estate_settings",
    "read_plan",
    "reread",
    "spec_texts",
    "stop_set",
]
