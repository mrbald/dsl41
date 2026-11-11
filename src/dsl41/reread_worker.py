"""Engine-owned source preparation in a separate interpreter (DL-463)."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dsl41.ir import CatalogIR
from dsl41.lint import lint_summary_lines
from dsl41.main_config import fingerprint, policy_report, prepare
from dsl41.reload_diff import (
    FieldDifference,
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
    box_members,
    reread,
    restart_boxes,
    stop_set,
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


def preview(
    candidate: Any,
    live: Live,
    *,
    include_definition_changes: bool = True,
    source_fingerprint: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A passive diff and the forecast at reread time, never an admission (DL-463)."""
    plan = diff(live.catalog, candidate.catalog, live.settings, candidate.settings)
    roots = restart_boxes(plan, live, candidate.catalog)
    members = box_members(live.catalog, roots) | box_members(candidate.catalog, roots)
    marked = candidate.metadata["non_idempotent"]
    boxes = {
        name
        for name in set(roots) | members
        if name in live.catalog.jobs
        and live.catalog.jobs[name].job_type == "BOX"
        and live.status.get(name) == "RUNNING"
    }
    stops = sorted(set(stop_set(plan, live, candidate.catalog)) | boxes)
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
    effects = []
    for name in stops:
        action = (
            "remove"
            if name in plan.removed
            else "hold"
            if name in marked
            else "restart_box"
            if name in boxes
            else "rerun"
        )
        effects.append({"job": name, "action": action})
    effects += [
        {"job": name, "action": "hold"}
        for name in sorted(members & set(marked))
        if name not in stops and live.status.get(name) not in (None, "INACTIVE")
    ]
    actions = {item["job"]: item["action"] for item in effects}
    restarting = [
        box
        for box in roots
        if box in candidate.catalog.jobs
        and candidate.catalog.jobs[box].job_type == "BOX"
        and box not in marked
    ]
    unchanged = box_members(candidate.catalog, restarting) & set(plan.unchanged) - {
        item["name"] for item in changed
    }
    for name in sorted(unchanged):
        if actions.get(name) != "hold":
            actions[name] = "restart"
    effects = [{"job": name, "action": action} for name, action in actions.items()]
    marks = {
        item["name"]: {"change": "changed", "action": actions.get(item["name"])} for item in changed
    }
    marks.update(
        {name: {"change": "removed", "action": actions.get(name)} for name in plan.removed}
    )
    marks.update(
        {name: {"change": "unchanged", "action": actions[name]} for name in sorted(unchanged)}
    )
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
                *[name for name in plan.unchanged if old.get(name) != origins.get(name)],
            ]
        )
    ]
    result = {
        "added": list(plan.added),
        "removed": list(plan.removed),
        "changed": changed,
        "unchanged": len(set(plan.unchanged) - {item["name"] for item in changed}),
        "stop": stops,
        "effects": effects,
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


def run(request: dict[str, Any]) -> dict[str, Any]:
    raw = request["live"]
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
