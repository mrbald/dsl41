"""Engine-owned source preparation in a separate interpreter (DL-463)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from dsl41.ir import CatalogIR
from dsl41.main_config import policy_report, prepare
from dsl41.reload_diff import diff, running_boxes
from dsl41.reload_plan import (
    Live,
    RereadRefused,
    preflight_items,
    box_members,
    reread,
    restart_boxes,
    stop_set,
)


def preview(candidate: Any, live: Live) -> dict[str, Any]:
    """A passive diff and the forecast at reread time, never an admission (DL-463)."""
    plan = diff(live.catalog, candidate.catalog, live.settings, candidate.settings)
    roots = restart_boxes(plan, live, candidate.catalog)
    members = box_members(live.catalog, roots) | box_members(candidate.catalog, roots)
    marked = candidate.metadata.get("non_idempotent", {})
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
    if candidate.metadata:
        policy, report = policy_report(candidate)
        messages = [policy.describe(), *[item.render() for item in report.violations]]
        if live.launch != candidate.metadata["launch"]:
            messages.append("host launch settings changed; restart before update")
    messages.extend(
        f"preflight {item.severity} [{item.code}]"
        f"{f' {item.job}' if item.job else ''}: {item.message}"
        for item in preflight_items(candidate, None, live.path_environment)
    )
    old = {name: job.span.file for name, job in live.catalog.jobs.items() if job.span}
    origins = candidate.metadata.get("origins", {})
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
    return {
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
        "diagnostics": candidate.metadata.get("diagnostics", []),
        "source_changes": sources,
        "settings_sources": list(
            dict.fromkeys(
                record["path"]
                for record in candidate.metadata.get("records", [])
                if live.settings != candidate.settings
            )
        ),
    }


def run(request: dict[str, Any]) -> dict[str, Any]:
    raw = request["live"]
    live = Live(**{**raw, "catalog": CatalogIR.model_validate(raw["catalog"])})
    path = Path(request["source"])
    candidate = prepare(path)
    result = {
        "ok": True,
        "live_rev": live.revision,
        "source": {"kind": "main", "path": str(path)},
        "preview": preview(candidate, live),
    }
    if request["update"]:
        from dsl41.runner_update import open_plan

        prepared = reread(
            path.parent, live, Path(request["run_root"]) / "plans", candidate=candidate
        )
        open_plan(Path(request["run_root"]), prepared.token)
        result["token"] = prepared.token
    return result


def main() -> None:
    result_fd = os.dup(1)
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
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
