"""The reload diff: pure functions over two catalogs (reread, spec-reload steps 2, 3 and 5).

`diff` compares a live `CatalogIR` with a candidate by job name. A job is changed when its
definition hash differs. The hash covers the job's normalized effective definition: the lowered
`JobIR` without positions, plus the normalized definition of every calendar, cycle, machine and
resource it references, plus the estate settings the caller passes (timezone, alias map,
semantic switches, cross-instance types). A calendar edit therefore changes every job that
references it, and the `Change` says so. A change in a setting changes every job.

Positional fields dropped before hashing (they move with whitespace and comments, never with
meaning):

- `JobIR.span`, `MachineIR.span`, `ResourceIR.span`, `CalendarIR.span`, `CycleIR.span`:
  `SourceSpan` (file, lines, byte offsets).
- `CondAttr.span` on `Semantics.condition`, `box_success` and `box_failure`: `SourceSpan`.
- `span` on every node of a condition tree (`StatusAtom`, `ExitCodeAtom`, `GlobalAtom`, `And`,
  `Or`, `Paren`): `CondSpan`, char offsets into the condition text.

`JobIR` carries no comment field: the scanner keeps comments on the AST, and lowering does not
copy them. `VarSite.start` and `end` are offsets into the stored attribute value, not into the
file, so they stay. `Lookback.raw` is the source token and stays: `1:00` and `01:00` are
written differently. No runner code reads it, so a respelling marks the job changed; that is the
safe direction. A redundant `Paren` marks it changed for the same reason.

A reference is a name in a job. The calendar namespace is `schedule.run_calendar` and
`exclude_calendar`, then an extended calendar's `holcal` (a calendar) and `cyccal` (a cycle).
Machines are the comma list in `exec_.machine`, then a pool's member names. Resources are
`resources` groups. A name with no definition hashes as absent, so adding the definition later
changes the job.

`plan_token` takes the candidate as a sequence of (name, bytes) parts: `estate.toml`, the JIL
files in the order the estate names them, and `non-idempotent.toml`; an absent
`non-idempotent.toml` is a part with empty bytes, not a missing part. The function digests the
list of names with the hash of each part, so moving bytes between parts changes the token.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dsl41.canon import digest
from dsl41.ir import CatalogIR, JobIR, unquote_jil_value

#: The status string a running row carries in the status map.
RUNNING = "RUNNING"

_COND_ATTRS = ("condition", "box_success", "box_failure")

#: Child keys of a dumped condition node that hold more nodes.
_COND_CHILDREN = ("operands", "inner")


@dataclass(frozen=True)
class Change:
    """One changed job. `reason` is set when only a referenced definition changed."""

    name: str
    old_hash: str
    new_hash: str
    reason: str | None = None


@dataclass(frozen=True)
class Plan:
    """The diff of a live catalog and a candidate; every name tuple is sorted.

    `prior_boxes` is (job, live box name or None) for each removed or changed job. It lets
    `running_boxes` see a move out of a box, which the candidate alone cannot show.
    `edited_boxes` names the removed jobs that are boxes in the live catalog. A changed box
    whose members stay the same is not structural; the stop path covers it.
    """

    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[Change, ...]
    unchanged: tuple[str, ...]
    prior_boxes: tuple[tuple[str, str | None], ...]
    edited_boxes: tuple[str, ...]


def _strip_cond(node: dict[str, Any]) -> None:
    node.pop("span", None)
    for key in _COND_CHILDREN:
        child = node.get(key)
        if isinstance(child, list):
            for item in child:
                _strip_cond(item)
        elif isinstance(child, dict):
            _strip_cond(child)


def _job_record(job: JobIR) -> dict[str, Any]:
    """The lowered job as plain data, without any source position."""
    exclude: dict[str, Any] = {"span": True}
    exclude["sem"] = {attr: {"span": True} for attr in _COND_ATTRS}
    record = job.model_dump(mode="json", exclude=exclude)
    for attr in _COND_ATTRS:
        attr_dump = record["sem"].get(attr)
        if attr_dump is not None:
            _strip_cond(attr_dump["cond"])
    return record


def _split_machines(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _references(job: JobIR, catalog: CatalogIR) -> dict[str, dict[str, Any] | None]:
    """Normalized definition of each referenced entity, keyed `kind:name`; None when undefined."""
    refs: dict[str, dict[str, Any] | None] = {}

    def calendar(name: str) -> None:
        key = f"calendar:{name}"
        if key in refs:
            return
        cal = catalog.calendars.get(name)
        refs[key] = None if cal is None else cal.model_dump(mode="json", exclude={"span"})
        if cal is not None and cal.kind == "extended":
            hol = unquote_jil_value(cal.attrs.get("holcal", ""))
            if hol:
                calendar(hol)
            cyc = unquote_jil_value(cal.attrs.get("cyccal", ""))
            if cyc:
                cycle(cyc)

    def cycle(name: str) -> None:
        key = f"cycle:{name}"
        if key not in refs:
            cyc = catalog.cycles.get(name)
            refs[key] = None if cyc is None else cyc.model_dump(mode="json", exclude={"span"})

    def machine(name: str) -> None:
        key = f"machine:{name}"
        if key in refs:
            return
        mach = catalog.machines.get(name)
        refs[key] = None if mach is None else mach.model_dump(mode="json", exclude={"span"})
        if mach is not None:
            for member in mach.members:
                machine(member.name)

    if job.schedule is not None:
        for ref in (job.schedule.run_calendar, job.schedule.exclude_calendar):
            if ref:
                calendar(ref)
    if job.exec_ is not None:
        for name in _split_machines(job.exec_.machine):
            machine(name)
    for group in job.resources:
        key = f"resource:{group.name}"
        res = catalog.resources.get(group.name)
        refs[key] = None if res is None else res.model_dump(mode="json", exclude={"span"})
    return refs


def definition_hash(job: JobIR, catalog: CatalogIR, estate_settings: Mapping[str, object]) -> str:
    """Digest of the job's normalized effective definition within `catalog`.

    `estate_settings` is canonicalised into the hash input. The caller (reread) supplies the
    values that shape a job's behaviour but live outside the catalog; this module only fixes
    the shape: a mapping of setting name to a canonical-encodable value.
    """
    return digest(
        {
            "job": _job_record(job),
            "refs": _references(job, catalog),
            "settings": dict(estate_settings),
        }
    )


_ABSENT = object()


def _reason(
    old: JobIR,
    live: CatalogIR,
    live_settings: Mapping[str, object],
    new: JobIR,
    candidate: CatalogIR,
    candidate_settings: Mapping[str, object],
) -> str | None:
    """The indirect cause when the job's own record is the same; None when the job was edited."""
    if _job_record(old) != _job_record(new):
        return None
    old_refs = _references(old, live)
    new_refs = _references(new, candidate)
    reasons: list[str] = []
    for key in sorted(old_refs.keys() | new_refs.keys()):
        if old_refs.get(key) == new_refs.get(key):
            continue
        kind, _, name = key.partition(":")
        if key not in new_refs:
            verb = "no longer referenced"
        elif key not in old_refs:
            verb = "now referenced"
        else:
            verb = "changed"
        reasons.append(f"{kind} {name} {verb}")
    for setting in sorted(live_settings.keys() | candidate_settings.keys()):
        if live_settings.get(setting, _ABSENT) != candidate_settings.get(setting, _ABSENT):
            reasons.append(f"estate setting {setting} changed")
    return "; ".join(reasons)


def diff(
    live: CatalogIR,
    candidate: CatalogIR,
    live_settings: Mapping[str, object],
    candidate_settings: Mapping[str, object],
) -> Plan:
    """Compare by job name: added, removed, changed (hash differs), unchanged.

    A difference between the two settings mappings changes every kept job.
    """
    live_names = live.jobs.keys()
    cand_names = candidate.jobs.keys()
    added = tuple(sorted(cand_names - live_names))
    removed = tuple(sorted(live_names - cand_names))
    changed: list[Change] = []
    unchanged: list[str] = []
    prior: list[tuple[str, str | None]] = [(name, live.jobs[name].box.box_name) for name in removed]
    for name in sorted(live_names & cand_names):
        old, new = live.jobs[name], candidate.jobs[name]
        old_hash = definition_hash(old, live, live_settings)
        new_hash = definition_hash(new, candidate, candidate_settings)
        if old_hash == new_hash:
            unchanged.append(name)
            continue
        changed.append(
            Change(
                name,
                old_hash,
                new_hash,
                _reason(old, live, live_settings, new, candidate, candidate_settings),
            )
        )
        prior.append((name, old.box.box_name))
    return Plan(
        added=added,
        removed=removed,
        changed=tuple(changed),
        unchanged=tuple(unchanged),
        prior_boxes=tuple(sorted(prior)),
        edited_boxes=tuple(name for name in removed if live.jobs[name].job_type == "BOX"),
    )


def running_boxes(plan: Plan, candidate: CatalogIR, status: Mapping[str, str]) -> tuple[str, ...]:
    """Boxes the plan edits structurally while `status` says the box is RUNNING.

    A box is edited when its member set changes (a member is added, a member is removed, or a
    changed job moves in or out) or when the plan removes the box itself. A box whose own
    attributes or referenced calendar change, with the same members, is not flagged here.
    """
    touched: set[str] = set(plan.edited_boxes)
    for name in plan.added:
        box = candidate.jobs[name].box.box_name
        if box is not None:
            touched.add(box)
    prior = dict(plan.prior_boxes)
    for name in plan.removed:
        old_box = prior.get(name)
        if old_box is not None:
            touched.add(old_box)
    for change in plan.changed:
        old_box = prior.get(change.name)
        new_box = candidate.jobs[change.name].box.box_name
        if old_box != new_box:
            touched.update(box for box in (old_box, new_box) if box is not None)
    return tuple(sorted(box for box in touched if status.get(box) == RUNNING))


def plan_token(parts: Sequence[tuple[str, bytes]], live_rev: int) -> str:
    """Hex digest of the candidate's named parts and the live catalog revision.

    `parts` is the whole candidate as (name, bytes): `estate.toml`, the JIL files in order, and
    `non-idempotent.toml` (empty bytes when absent). The token names a directory, so it carries
    no algorithm prefix and no colon.
    """
    full = digest(
        {
            "parts": [
                {"name": name, "sha256": hashlib.sha256(data).hexdigest()} for name, data in parts
            ],
            "live_rev": live_rev,
        }
    )
    return full.removeprefix("sha256:")


__all__ = [
    "RUNNING",
    "Change",
    "Plan",
    "definition_hash",
    "diff",
    "plan_token",
    "running_boxes",
]
