"""Prepared operator views for status and reload output (DL-482)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from dsl41.display_time import format_local_at
from dsl41.oracle_state import FAILED, LIVE
from dsl41.runner_control import STATUS_FLAG_MARKS, status_label, status_flags


class OutputStyle(str, Enum):
    focused = "focused"
    neutral = "neutral"


def one_line(value: Any) -> str:
    """Escape text controls so one value cannot create another output line (DL-482)."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)[1:-1].translate(
            {0x85: r"\u0085", 0x2028: r"\u2028", 0x2029: r"\u2029"}
        )
    return "-" if value is None else str(value)


def counted(count: int, noun: str = "job") -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def bounded_text(value: str, limit: int = 240) -> str:
    return value if len(value) <= limit else value[: limit - 5] + "[cut]"


def detail_lines(value: Any, key: str = "") -> list[str]:
    """Keep every single-job field and convert only instants (DL-473, DL-482)."""
    if isinstance(value, dict) and value:
        return [
            line
            for field, child in value.items()
            for line in detail_lines(child, f"{key}.{field}" if key else field)
        ]
    if isinstance(value, list) and value:
        return [
            line
            for index, child in enumerate(value)
            for line in detail_lines(child, f"{key}[{index}]")
        ]
    if key.rsplit(".", 1)[-1] in {"status_at", "due", "started_at", "ended_at"}:
        shown = one_line(format_local_at(value))
    else:
        shown = one_line(value) if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return [f"{one_line(key)}: {shown}"]


def names_text(names: Sequence[str]) -> str:
    shown = [one_line(name) for name in names[:3]]
    return ", ".join(shown) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def table_lines(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [
        max([len(header), *(len(row[i]) for row in rows)]) for i, header in enumerate(headers)
    ]
    return [
        "  ".join(f"{cell:<{width}}" for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in [headers, *rows]
    ]


@dataclass(frozen=True)
class StatusView:
    header: str
    counts: tuple[tuple[str, int], ...]
    flags: tuple[tuple[str, tuple[str, ...]], ...]
    rows: tuple[tuple[str, ...], ...]
    sections: tuple[tuple[str, tuple[str, ...]], ...]
    notices: tuple[str, ...]


def prepare_status(answer: Mapping[str, Any]) -> StatusView:
    """Partition committed statuses; flags and STOPPING do not change counts (DL-482).

    LIVE is running, FAILED is failed, QUE_WAIT is queued. Ice, noexec and armed
    stay in their status and appear as flags. STOPPING stays in its committed
    status and is also an attention section, including cleanup after SUCCESS.
    """
    counts: Counter[str] = Counter()
    flags: dict[str, list[str]] = {key: [] for _, key in STATUS_FLAG_MARKS}
    sections: dict[str, list[str]] = defaultdict(list)
    rows = []
    forecasts: dict[str, list[str]] = defaultdict(list)
    jobs = answer.get("jobs", {})
    for name, row in sorted(jobs.items()):
        state = row.get("status")
        group = (
            "running"
            if state in LIVE
            else "failed"
            if state in FAILED
            else "queued"
            if state == "QUE_WAIT"
            else "success"
            if state == "SUCCESS"
            else "inactive"
            if state == "INACTIVE"
            else "other"
        )
        counts[group] += 1
        for _, key in STATUS_FLAG_MARKS:
            if row.get(key):
                flags[key].append(name)
        marks = status_flags(row) or "-"
        at = one_line(format_local_at(row.get("status_at")))
        label = one_line(status_label(row))
        rows.append(
            tuple(
                map(
                    one_line,
                    (
                        name,
                        row.get("job_type"),
                        status_label(row),
                        format_local_at(row.get("status_at")),
                        row.get("run_number", 0),
                        row.get("exit_code"),
                        marks,
                        row.get("box_name"),
                        row.get("state_rev", 0),
                    ),
                )
            )
        )
        line = f"{one_line(name)}   {label}"
        if group == "failed":
            line += (
                f"   exit {one_line(row.get('exit_code'))}   {at}   {one_line(row.get('log_err'))}"
            )
        else:
            line += f"   since {at}"
            if row.get("box_name") is not None:
                line += f"   box {one_line(row['box_name'])}"
        attempt = row.get("attempt") or {}
        if attempt.get("state") == "STOPPING" or attempt.get("held") or row.get("interrupted"):
            sections["stopping or interrupted"].append(line)
        elif group in {"failed", "running", "queued", "other"}:
            sections[group].append(line)
        elif row.get("reread"):
            mark = row["reread"]
            label = str(mark["change"]) + ("; " + mark["action"] if mark.get("action") else "")
            forecasts[label].append(name)
    notices = []
    if answer.get("spec_drift"):
        # Status carries no reload capability; advice would guess the launch mode (DL-482).
        notices.append("estate files on disk differ from the running estate")
    elif answer.get("spec_drift") is None:
        notices.append("disk comparison unavailable")
    if answer.get("update"):
        notices.append("update pending; waiting for: " + names_text(answer["update"]["stopping"]))
    notices += [
        f"reread forecast: {one_line(label)} {len(names)}: {names_text(names)}"
        for label, names in forecasts.items()
    ]
    header = (
        f"engine {one_line(answer.get('engine'))}   estate revision "
        f"{one_line(answer.get('config_rev'))}   {counted(len(jobs))}"
    )
    labels = {"on_hold": "on hold", "on_ice": "on ice", "on_noexec": "noexec", "armed": "armed"}
    return StatusView(
        header,
        tuple(
            (key, counts[key])
            for key in ("running", "queued", "failed", "success", "inactive", "other")
            if key != "other" or counts[key]
        ),
        tuple((labels[key], tuple(flags[key])) for _, key in STATUS_FLAG_MARKS),
        tuple(rows),
        tuple(
            (key, tuple(sections[key]))
            for key in (
                "failed",
                "running",
                "queued",
                "other",
                "stopping or interrupted",
                "reread forecast",
            )
            if sections[key]
        ),
        tuple(notices),
    )


def status_lines(
    answer: Mapping[str, Any],
    style: OutputStyle,
    *,
    full: bool = False,
    only_row: str | None = None,
) -> list[str]:
    view = prepare_status(answer)
    if only_row is not None:
        rows = [row for row in view.rows if row[0] == one_line(only_row)]
        return table_lines(
            ("JOB", "TYPE", "STATUS", "SINCE", "RUN", "EXIT", "FLAGS", "BOX", "REV"), rows
        )[1:]
    lines = [view.header, *view.notices]
    if full:
        return [*lines, "", *detail_lines(dict(answer))]
    lines += [
        "",
        "   ".join(f"{name} {count}" for name, count in view.counts),
        "flags: " + "   ".join(f"{name} {len(names)}" for name, names in view.flags),
    ]
    if not view.rows:
        return [*lines, "no jobs"]
    if style is OutputStyle.neutral:
        return [
            *lines,
            "",
            *table_lines(
                ("JOB", "TYPE", "STATUS", "SINCE", "RUN", "EXIT", "FLAGS", "BOX", "REV"), view.rows
            ),
        ]
    for title, entries in view.sections:
        lines += ["", title, *("  " + entry for entry in entries)]
    for title, names in view.flags:
        if names:
            lines += ["", title, "  " + names_text(names)]
    return lines


def change_text(group: Mapping[str, Any]) -> str:
    if group.get("redacted"):
        return f"{one_line(group['field'])}: changed (values hidden)"
    before, after = group["before"], group["after"]
    values = f"{before} -> {after}"
    if group["complex"]:
        values = "changed" + (
            f" ({values})" if len(values) <= 120 and "[cut]" not in values else ""
        )
    return f"{one_line(group['field'])}: {values}"


@dataclass(frozen=True)
class ReloadView:
    header: str
    summary: str
    groups: tuple[tuple[str, tuple[str, ...]], ...]
    rows: tuple[str, ...]
    footer: tuple[str, ...]
    effects: tuple[tuple[str, tuple[str, ...]], ...]
    effect_index: int


def prepare_reread(answer: Mapping[str, Any], apply_command: str) -> ReloadView:
    view = answer["preview"]
    changed = view["changed"]
    details = view.get("definition_changes")
    count = len(changed)
    groups = []
    rows: dict[str, list[str]] = defaultdict(list)
    for group in (details or {}).get("groups", []):
        text = change_text(group)
        names = tuple(group["jobs"])
        groups.append((text, names))
        for name in names:
            rows[name].append(text)
        if not names:
            rows["shared settings"].append(text)
    fallback: dict[str, list[str]] = defaultdict(list)
    for item in changed:
        if item["name"] not in rows:
            if (
                details
                and details.get("settings_changed")
                and not details.get("omitted")
                and item.get("reason") != "membership"
            ):
                text = "affected by shared estate settings"
            elif details and details.get("omitted"):
                text = "details not shown (size limit)"
            else:
                text = one_line(
                    item.get("reason") or "definition changed; attribute details unavailable"
                )
            rows[item["name"]].append(text)
            fallback[text].append(item["name"])
    groups += [(text, tuple(names)) for text, names in fallback.items()]
    for kind in ("added", "removed"):
        if view[kind]:
            groups.append((kind, tuple(view[kind])))
            for name in view[kind]:
                rows[name].append("definition updated" if kind == "changed" else kind)
    # Source paths are grouped once, including moves that changed no definition (DL-482).
    sources: dict[str, list[str]] = defaultdict(list)
    for item in view["source_changes"]:
        if item["before"] != item["after"]:
            text = f"source: {one_line(item['before'])} -> {one_line(item['after'])}"
            sources[text].append(item["job"])
            rows[item["job"]].append(text)
    groups += [(text, tuple(names)) for text, names in sources.items()]
    for item in view["restored"]:
        text = "re-added with its earlier flags: " + ", ".join(item["flags"])
        rows[item["name"]].append(text)
        groups.append((text, (item["name"],)))
    footer = []
    jobs_changed = bool(count or view["added"] or view["removed"])
    settings_changed = (details or {}).get(
        "settings_changed",
        any(group.get("scope") == "settings" for group in (details or {}).get("groups", [])),
    )
    if not jobs_changed and not settings_changed and (details or {}).get("sources_changed"):
        text = "estate files changed, but no job or setting did"
        if not (details or {}).get("blocked"):
            text += "; update still applies them"
        footer.append(text)
    elif (
        not jobs_changed and not settings_changed and (details or {}).get("sources_changed") is None
    ):
        footer.append("no job or setting changes; estate file comparison unavailable")
    if (details or {}).get("omitted"):
        footer.append(f"{counted(details['omitted'], 'more difference')} not shown")
    if view["settings_sources"] and (
        details is None or any(group.get("scope") == "settings" for group in details["groups"])
    ):
        footer.append(
            "shared settings sources: " + ", ".join(map(one_line, view["settings_sources"]))
        )
    stops = view["stop"]
    footer.append(
        "if applied now: "
        + (
            f"stopping {counted(len(stops), 'running job')}: {names_text(stops)}"
            if stops
            else "no running job is stopped"
        )
    )
    effect_index = len(footer)
    effects = {
        "rerun": "would rerun under the new definition",
        "hold": "would be ON_HOLD (marked as not safe to rerun)",
        "remove": "would be removed; no replacement",
        "restart_box": "would restart as a new box run",
        "restart": "would restart with its box",
    }
    effect_groups: dict[str, list[str]] = defaultdict(list)
    for effect in view["effects"]:
        effect_groups[effects[effect["action"]]].append(effect["job"])
    for item in view["non_idempotent"]:
        footer.append(
            f"marked non-idempotent: {one_line(item['job'])} ({one_line(item['reason'])})"
        )
    paths = sorted(
        {
            item["after"] or item["before"]
            for item in view["source_changes"]
            if item["after"] or item["before"]
        }
    )
    footer.append(
        bounded_text("read from: " + ", ".join(map(one_line, paths or [answer["source"]["path"]])))
    )
    if details is None:
        footer.append("update readiness unavailable from this engine")
    elif details["blocked"]:
        reported = {*view["lint_messages"], *view["diagnostics"]}
        footer += [
            "update would refuse: " + one_line(reason)
            for reason in details["blocked"]
            if reason not in reported
        ]
        if any(reason in reported for reason in details["blocked"]):
            footer.append("update would refuse: blocking findings are reported on stderr")
    else:
        footer.append("apply with: " + apply_command)
    footer.append("forecast at reread time; update rereads before applying")
    if (
        not jobs_changed
        and not settings_changed
        and (details or {}).get("sources_changed") is False
    ):
        header = "no job, setting or estate file changed"
    elif jobs_changed or settings_changed or (details or {}).get("sources_changed"):
        header = f"estate on disk differs from the running estate (revision {answer['live_rev']})"
    else:
        header = (
            f"no job definition changes against the running estate (revision {answer['live_rev']})"
        )
    return ReloadView(
        header,
        f"changed {counted(count)}, added {len(view['added'])}, removed {len(view['removed'])}, unchanged {view['unchanged']}",
        tuple(groups),
        tuple(
            f"{one_line(name)}   {'; '.join(changes)}"
            for name, changes in sorted(
                rows.items(), key=lambda item: (item[0] != "shared settings", item[0])
            )
        ),
        tuple(footer),
        tuple((text, tuple(names)) for text, names in effect_groups.items()),
        effect_index,
    )


def reread_lines(answer: Mapping[str, Any], style: OutputStyle, apply_command: str) -> list[str]:
    view = prepare_reread(answer, apply_command)
    lines = [view.header, "", view.summary]
    if style is OutputStyle.neutral:
        lines += ["  " + row for row in view.rows]
    else:
        preview = answer["preview"]
        total = len(preview["changed"]) + len(preview["added"]) + preview["unchanged"]
        for text, names in view.groups:
            all_changed = (
                total
                and len(names) == total
                and len(names) != 1
                and text not in {"added", "removed"}
                and not text.startswith("source:")
            )
            suffix = counted(len(names)) + (" (all)" if all_changed else ": " + names_text(names))
            lines.append(f"  {text}   {suffix}" if names else f"  {text}")
    impact = []
    for text, names in view.effects:
        if style is OutputStyle.neutral or len(names) == 1:
            impact += [f"  {one_line(name)}   {text}" for name in names]
        else:
            impact.append(f"  {text}   {counted(len(names))}: {names_text(names)}")
    # Keep effects beside the stop forecast, before sources and admission advice (DL-482).
    footer = list(view.footer)
    footer[view.effect_index : view.effect_index] = impact
    return [*lines, "", *footer]


def application_lines(application: Any, style: OutputStyle) -> list[str]:
    """Both update layouts use the application's names and recorded choices (DL-482)."""
    outcomes = {
        "complete": "its own result stands",
        "rerun": "stopped, rerun under the new definition",
        "hold": "stopped, now ON_HOLD (automatic rerun withheld)",
        "none": "no rerun or hold follows from this stop",
    }
    rows: dict[str, list[str]] = defaultdict(list)
    for kind in ("changed", "added", "removed"):
        for name in getattr(application, kind):
            rows[name].append("definition updated" if kind == "changed" else kind)
    for name, flags in application.restored:
        rows[name].append("re-added with its earlier flags: " + ", ".join(flags))
    for name, choice in application.stopped:
        rows[name].append(outcomes[choice])
    lines = [
        f"applied: catalog revision {application.rev} ({application.digest})",
        f"  changed {counted(len(application.changed))}, added {len(application.added)}, removed {len(application.removed)}",
    ]
    groups: dict[str, list[str]] = defaultdict(list)
    for name, choice in application.stopped:
        groups[outcomes[choice]].append(name)
    for name, flags in application.restored:
        groups["re-added with its earlier flags: " + ", ".join(flags)].append(name)
    if style is OutputStyle.neutral:
        lines += [
            f"  {one_line(name)}   {'; '.join(texts)}" for name, texts in sorted(rows.items())
        ]
    else:
        for text, names in groups.items():
            lines.append(
                f"  {one_line(names[0])}   {text}"
                if len(names) == 1
                else f"  {text}   {counted(len(names))}: {names_text(names)}"
            )
    return lines
