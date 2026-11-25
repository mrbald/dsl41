"""Engine-owned source preparation in a separate interpreter (DL-463)."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dsl41.ir import CatalogIR, check_ir_version
from dsl41.lint import lint_summary_lines
from dsl41.main_config import fingerprint, policy_report, prepare
from dsl41.reload_diff import (
    FieldDifference,
    Plan,
    definition_record,
    difference_view,
    diff,
    record_difference,
    running_boxes,
)
from dsl41.reload_plan import (
    Live,
    check_reload_candidate,
    RereadRefused,
    preflight_items,
    reread,
    stop_runs,
    stop_set,
    tree_active,
)


# Leave room for the engine-state advice added after the worker answers (DL-482).
DIFFERENCE_BYTE_BUDGET = 1024 * 1024


class _DifferenceBudget:
    """Bound serialized difference data, including grouped job memberships (DL-482)."""

    def __init__(self, metadata: dict[str, Any]) -> None:
        reasons = metadata["blocked"]
        metadata["blocked"] = [
            reason if len(reason) <= 120 else reason[:115] + "[cut]" for reason in reasons[:20]
        ]
        if len(reasons) > 20:
            metadata["blocked"].append(
                f"{len(reasons) - 20} more blocking finding"
                + ("" if len(reasons) == 21 else "s")
                + " not shown"
            )
        self.result = {**metadata, "groups": [], "omitted": 0}
        self.groups: dict[str, dict[str, Any]] = {}
        self.used = len(json.dumps(self.result).encode()) + 512
        self.full = False

    def add(self, change: Any, job: str | None) -> None:
        if self.full:
            self.result["omitted"] += 1
            return
        view = difference_view(change)
        key = json.dumps(view, sort_keys=True)
        group = self.groups.get(key)
        cost = (
            len(json.dumps(job).encode()) + 2
            if group is not None
            else len(json.dumps({**view, "jobs": [job] if job is not None else []}).encode()) + 2
        )
        if self.used + cost > DIFFERENCE_BYTE_BUDGET:
            self.full = True
            self.result["omitted"] += 1
            return
        self.used += cost
        if group is None:
            group = {**view, "jobs": []}
            self.groups[key] = group
            self.result["groups"].append(group)
        if job is not None:
            group["jobs"].append(job)


def definition_changes(
    candidate: Any,
    live: Live,
    *,
    reported: Sequence[str] = (),
    source_fingerprint: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Stream bounded differences; settings have no affected-job attribution (DL-482)."""
    settings: list[FieldDifference] = []
    for change in record_difference(live.settings, candidate.settings, ("settings",)):
        if change.path == ("settings", "job_env"):
            before_env = change.before if isinstance(change.before, Mapping) else {}
            after_env = change.after if isinstance(change.after, Mapping) else {}
            settings.extend(record_difference(before_env, after_env, change.path))
        else:
            settings.append(change)
    try:
        check_reload_candidate(candidate, live)
        blocked: Sequence[str] = ()
    except RereadRefused as exc:
        blocked = exc.findings
    existing = set(reported)
    reasons = [reason for reason in blocked if reason not in existing]
    repeated = sum(reason in existing for reason in blocked)
    if repeated:
        reasons.append(
            f"{repeated} blocking finding"
            + (" is" if repeated == 1 else "s are")
            + " reported on stderr"
        )
    budget = _DifferenceBudget(
        {
            "blocked": reasons,
            "settings_changed": bool(settings),
            "sources_changed": fingerprint(candidate) != source_fingerprint
            if source_fingerprint
            else None,
        }
    )
    for change in settings:
        budget.add(change, None)
    for name in sorted(live.catalog.jobs.keys() & candidate.catalog.jobs.keys()):
        before = definition_record(live.catalog.jobs[name], live.catalog, live.settings)
        after = definition_record(
            candidate.catalog.jobs[name], candidate.catalog, candidate.settings
        )
        before["settings"], after["settings"] = {}, {}
        changes = record_difference(before, after)
        direct = any(change.path[0] == "job" for change in changes)
        for change in changes:
            if (
                change.path[0] == "refs"
                and direct
                and (change.path[1] not in before["refs"] or change.path[1] not in after["refs"])
            ):
                continue
            budget.add(change, name)
    return budget.result


def _seed_values(live: CatalogIR, candidate: CatalogIR, name: str) -> dict[str, str]:
    """The display strings of a seed-only job's two `status:` values (DL-482, DL-533)."""
    view = difference_view(
        FieldDifference(
            ("job", "sem", "initial_status"),
            live.jobs[name].sem.initial_status,
            candidate.jobs[name].sem.initial_status,
        )
    )
    return {"before": view["before"], "after": view["after"]}


def _tree_active(candidate: Any, live: Live, plan: Plan) -> tuple[str, ...]:
    """The `tree_active` refusal the update would give, from the same function and inputs as
    its admission; none when its preparation refuses first (a blocking lint finding, changed
    host launch settings, a preflight ERROR), as the update meets that refusal before it
    reaches the admission (DL-559)."""
    try:
        check_reload_candidate(candidate, live)
    except RereadRefused:
        return ()
    stops = tuple(stop_runs(plan, live))
    return tree_active(live.catalog, candidate.catalog, live.status, live.owned, stops)


def preview(
    candidate: Any,
    live: Live,
    *,
    include_definition_changes: bool = True,
    source_fingerprint: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A passive diff and the forecast at reread time, never an admission (DL-463)."""
    plan = diff(live.catalog, candidate.catalog, live.settings, candidate.settings)
    marked = candidate.metadata["non_idempotent"]
    stops = list(stop_set(plan, live))
    changed: list[dict[str, Any]] = [
        {"name": item.name, "reason": item.reason} for item in plan.changed
    ]
    structural = running_boxes(
        plan, candidate.catalog, {name: "RUNNING" for name in live.catalog.jobs}
    )
    changed += [
        {"name": name, "reason": "membership"}
        for name in structural
        if name not in plan.removed and name not in {item.name for item in plan.changed}
    ]
    # an update restarts no box, so every effect is a stop's (DL-559)
    effects = [
        {
            "job": name,
            "action": "hold"
            if name in marked and name not in plan.removed
            else "remove"
            if name in plan.removed
            else "rerun",
        }
        for name in stops
    ]
    actions = {item["job"]: item["action"] for item in effects}
    marks = {
        item["name"]: {"change": "changed", "action": actions.get(item["name"])} for item in changed
    }
    marks.update(
        {name: {"change": "removed", "action": actions.get(name)} for name in plan.removed}
    )
    refused = _tree_active(candidate, live, plan)
    messages: list[str] = []
    summary: list[str] = []
    suffix: list[str] = []
    policy, report = policy_report(candidate)
    messages = [policy.describe(), *[item.render() for item in report.violations]]
    if include_definition_changes:
        summary = [
            policy.describe(),
            *lint_summary_lines(
                report.violations,
                blocks=policy.blocks,
                config_path=candidate.metadata["config_path"],
            ),
        ]
    if live.launch != candidate.metadata["launch"]:
        suffix.append("host launch settings changed; restart before update")
    suffix.extend(
        f"preflight {item.severity} [{item.code}]"
        f"{f' {item.job}' if item.job else ''}: {item.message}"
        for item in preflight_items(candidate, None, live.path_environment)
    )
    messages.extend(suffix)
    old = {name: job.span.file for name, job in live.catalog.jobs.items() if job.span}
    origins = candidate.metadata["origins"]
    sources = [
        {"job": name, "before": old.get(name), "after": origins.get(name)}
        for name in dict.fromkeys(
            [
                *plan.added,
                *plan.removed,
                *[item["name"] for item in changed],
                *[item.name for item in plan.seed_only],
                *[name for name in plan.unchanged if old.get(name) != origins.get(name)],
            ]
        )
    ]
    # A seed-only job is counted on its own, not as changed or unchanged; its forecast is an
    # unchanged job's (DL-533).
    seeds = [
        {"name": item.name, **_seed_values(live.catalog, candidate.catalog, item.name)}
        for item in plan.seed_only
    ]
    result = {
        "added": list(plan.added),
        "removed": list(plan.removed),
        "changed": changed,
        "seed_only": seeds,
        "unchanged": len(
            set(plan.unchanged)
            - {item["name"] for item in changed}
            - {item["name"] for item in seeds}
        ),
        "stop": stops,
        "effects": effects,
        "update_refusal": {"code": "tree_active", "names": list(refused)} if refused else None,
        "marks": marks,
        "restored": [
            {"name": name, "flags": list(live.tombstones[name])}
            for name in plan.added
            if live.tombstones.get(name)
        ],
        "non_idempotent": [
            {"job": name, "reason": marked[name]} for name in stops if name in marked
        ],
        "lint_messages": messages,
        "diagnostics": candidate.metadata["diagnostics"],
        "source_changes": sources,
        "settings_sources": list(
            dict.fromkeys(
                record["path"]
                for record in candidate.metadata["records"]
                if live.settings != candidate.settings
            )
        ),
    }

    if include_definition_changes:
        result["lint_summary"] = [*summary, *suffix]
        result["definition_changes"] = definition_changes(
            candidate,
            live,
            reported=[*messages, *result["diagnostics"]],
            source_fingerprint=source_fingerprint,
        )
    return result


#: The engine and this worker are different builds when their IR-F versions differ: the package
#: was replaced under a running engine. The installed build refuses that engine's root, so the
#: remedy is the engine's own build or a fresh root (DL-561).
_WORKER_REMEDY = (
    "The engine runs another build than this worker: reread with the worker of the build the"
    " engine runs, or drain that engine and start the installed build on a fresh run root"
)


def run(request: dict[str, Any]) -> dict[str, Any]:
    raw = request["live"]
    # the engine's catalog is read as a stored one is (DL-561)
    check_ir_version(raw["catalog"], where="reread: the engine's catalog", remedy=_WORKER_REMEDY)
    live = Live(**{**raw, "catalog": CatalogIR.model_validate(raw["catalog"])})
    path = Path(request["source"])
    candidate = prepare(path)
    result = {
        "ok": True,
        "live_rev": live.revision,
        "source": {"kind": "main", "path": str(path)},
        "preview": preview(
            candidate,
            live,
            include_definition_changes=not request["update"],
            source_fingerprint=request.get("source_fingerprint"),
        ),
    }
    if request["update"]:
        from dsl41.runner_update import open_plan

        prepared = reread(live, Path(request["run_root"]) / "plans", candidate=candidate)
        open_plan(Path(request["run_root"]), prepared.token)
        result["token"] = prepared.token
    return result


def main() -> None:
    result_fd = os.dup(1)
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
    try:
        answer = run(json.load(sys.stdin))
    except RereadRefused as exc:
        answer = {"ok": False, "code": "invalid_argument", "error": "; ".join(exc.findings)}
    except Exception as exc:
        answer = {"ok": False, "code": "engine_error", "error": str(exc)}
    with os.fdopen(result_fd, "w") as output:
        json.dump(answer, output)
        output.write("\n")


if __name__ == "__main__":
    main()
