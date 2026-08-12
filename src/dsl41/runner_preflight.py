"""Runner preflight (ss8): refuse loudly, run honestly.

Split out of runner.py by DL-74, with the paragraph it owns, verbatim.

Phase 11c (ss5, ss8, ss10; DL-45 pins the decisions):

- Preflight (ss8): ERROR refuses the run (job-type / machine / owner /
  calendar / timezone / oracle construction), WARN prints + journals and
  runs (n-retrys DL-53 scope, resources, exhausted run_calendar DL-56,
  AND-success skeleton cycle -- cycles are
  legal AutoSys, DL-13/L010, so they only disable `plan`). Identity rules
  (machine/owner) guard real execution and are skipped for rehearse
  (execution=False): the FakeAdapter runs nothing.
"""

from __future__ import annotations

import getpass
import graphlib
import socket as socket_mod

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from dsl41.autocal import (
    CalendarRuleError,
    CompiledCalendar,
    compile_calendar,
    standard_days,
)
from dsl41.conditions import And, Cond, Paren, StatusAtom
from dsl41.ir import CatalogIR, JobIR, MachineIR, _unquote
from dsl41.oracle import Oracle, OracleError
from dsl41.runner_scheduler import _DAY_CODES, _city_candidates, resolve_timezone


# ------------------------------------------------------------------ preflight (ss8)

#: the runner's executable universe; anything else is a preflight ERROR
_RUNNABLE_TYPES = frozenset({"CMD", "BOX", "FW"})


class PreflightItem(BaseModel):
    """One ss8 finding. ERROR refuses the run; WARN prints, journals, and
    runs. Codes are stable kebab keys (fixture pair per rule, ss8)."""

    severity: Literal["ERROR", "WARN"]
    code: str
    job: str | None = None
    message: str


def _local_identity(declared: frozenset[str] = frozenset()) -> frozenset[str]:
    """The machine names this runner answers to (DL-52). `localhost` always
    counts. When the operator DECLARES identities (`--as-machine greezy_spoon`),
    THOSE are the identity -- pure and explicit, no hostname/FQDN guessing: the
    runner knows which estate machine it is, and a job pinned to that name (or
    resolving to it through insert_machine) runs here. Omitted, we fall back to
    the forward hostname (short + full) for zero-config, but NEVER reverse-DNS:
    `getfqdn()` can stall for tens of seconds and, worse, decides placement from
    what the OS resolver thinks this box is called -- a different namespace from
    the estate's machine names (DL-52 replaces DL-45 M6's FQDN matching)."""
    identity = {"localhost"}
    if declared:
        identity |= {d.strip().lower() for d in declared if d.strip()}
        return frozenset(identity)
    hostname = socket_mod.gethostname().lower()
    identity |= {hostname, hostname.split(".")[0]}
    return frozenset(identity)


#: Machine `type:` values the resolver understands (DL-49). Anything else --
#: including a missing type -- is refused, never guessed (Goal-1 plan).
_KNOWN_MACHINE_TYPES = frozenset({"a", "r", "n", "v"})

MachineVerdict = Literal["local", "foreign", "mixed", "error"]

MachinePolicy = Literal["strict", "local-eligible"]


@dataclass(frozen=True)
class MachineResolution:
    """Outcome of resolving a job's `machine:` through `insert_machine`
    (DL-49). `detail` carries the resolved host (local/foreign), a human
    reason (error), or the pool summary (mixed) -- straight into the
    preflight message and, later, the run journal / dispatch router."""

    verdict: MachineVerdict
    detail: str


def _node_name(machine: MachineIR) -> str | None:
    """The machine's node_name, semantically unquoted (a hostname never
    carries quotes) -- node_name feeds host resolution, so it must not be
    read as the raw opaque carry (review: quoted node_name false-refused)."""
    raw = machine.attrs.get("node_name")
    return _unquote(raw) if raw else None


def _leaf_host(machine: MachineIR) -> tuple[str | None, str | None]:
    """Effective host of a NON-virtual machine: (host, error). Agents must
    carry node_name (no guessing); real machines fall back to the record
    name. A missing/unknown type is a refusal, not a default."""
    kind = (machine.machine_type or "").lower()
    if kind == "a":
        node = _node_name(machine)
        if not node:
            return None, f"agent machine {machine.name!r} has no node_name to resolve its host"
        return node, None
    if kind in ("r", "n"):
        return (_node_name(machine) or machine.name), None
    if kind == "":
        return None, f"machine {machine.name!r} has no type (add type: a|r|n|v)"
    return None, f"machine {machine.name!r} has unsupported type {machine.machine_type!r}"


def resolve_machine(
    name: str, machines: Mapping[str, MachineIR], local: frozenset[str]
) -> MachineResolution:
    """Resolve a job's `machine:` name to a placement verdict against this
    runner's declared identity (DL-52 `local`), honouring `insert_machine`
    (DL-49). A DIRECT name match wins first: if the job names a machine this
    runner answers to, run here -- the operator's "this runner IS greezy_spoon"
    is authoritative over whatever node_name the record carries. Otherwise
    agents/real machines resolve through node_name; a virtual machine is local
    iff ALL members resolve local, foreign iff none, else mixed. Undefined and
    not-our-name is foreign. Bad definitions (missing type, undefined / nested /
    typeless member, empty pool) are `error`, never guessed."""
    if name.lower() in local:
        return MachineResolution("local", name)  # DL-52: named identity match
    machine = machines.get(name)
    if machine is None:
        return MachineResolution("foreign", name)  # undefined and not our name
    kind = (machine.machine_type or "").lower()
    if kind and kind not in _KNOWN_MACHINE_TYPES:
        return MachineResolution(
            "error", f"machine {name!r} has unsupported type {machine.machine_type!r}"
        )
    if kind != "v":
        host, err = _leaf_host(machine)
        if err is not None:
            return MachineResolution("error", err)
        assert host is not None
        return MachineResolution("local" if host.lower() in local else "foreign", host)
    # virtual machine: resolve every member leaf, then fold (any-of).
    if not machine.members:
        return MachineResolution("error", f"virtual machine {name!r} has no member machines")
    members_local: list[bool] = []
    for member in machine.members:
        leaf = machines.get(member.name)
        if leaf is None:
            return MachineResolution(
                "error", f"virtual machine {name!r} member {member.name!r} is not defined"
            )
        if (leaf.machine_type or "").lower() == "v":
            return MachineResolution(
                "error",
                f"virtual machine {name!r} member {member.name!r} is itself virtual (no nesting)",
            )
        host, err = _leaf_host(leaf)
        if err is not None:
            return MachineResolution("error", f"virtual machine {name!r}: {err}")
        assert host is not None
        members_local.append(host.lower() in local)
    count = len(machine.members)
    if all(members_local):
        return MachineResolution("local", f"virtual pool {name!r} ({count} member(s), all local)")
    if not any(members_local):
        return MachineResolution(
            "foreign", f"virtual pool {name!r} ({count} member(s), none local)"
        )
    return MachineResolution("mixed", f"virtual pool {name!r} ({count} member(s), some remote)")


def and_success_skeleton(catalog: CatalogIR) -> dict[str, set[str]]:
    """job -> success-predecessors reachable through AND/Paren spines only
    (an s() atom under an OR is an alternative, not a hard dependency).
    Instance-qualified and undefined references are skipped: pseudo-entries
    have no run to order. Shared by the ss8 cycle WARN and the ss10 `plan`
    view, so the two can never disagree about acyclicity."""

    def collect(cond: Cond, into: set[str]) -> None:
        if isinstance(cond, And):
            for op in cond.operands:
                collect(op, into)
        elif isinstance(cond, Paren):
            collect(cond.inner, into)
        elif (
            isinstance(cond, StatusAtom)
            and cond.status == "SUCCESS"
            and cond.job.instance is None
            and cond.job.name in catalog.jobs
        ):
            into.add(cond.job.name)

    skeleton: dict[str, set[str]] = {}
    for name, job in catalog.jobs.items():
        preds: set[str] = set()
        if job.sem.condition is not None:
            collect(job.sem.condition.cond, preds)
        skeleton[name] = preds
    return skeleton


def _resource_preflight(name: str, job: JobIR, catalog: CatalogIR) -> list[PreflightItem]:
    """DL-50: resource/load attributes are HONORED by the oracle (capacity
    buckets, QUE_WAIT, deterministic admission). Preflight refuses -- fail-
    closed -- only the shapes the oracle cannot model faithfully, in BOTH run
    and rehearse (resource semantics gate the oracle in either clock domain):
      * a `resources:` requirement whose resource is unsized (no insert_resource
        in the set, or no parseable `amount`) -- an unsized semaphore cannot be
        honored (stricter than L016's warn; a `--resource-capacity` override is
        a documented future escape hatch, not v1);
      * an unknown res_type (not R/D/T) -- unknown release semantics;
      * a malformed job_load/priority/max_load -- a non-integer load.
    It WARNs (does not refuse) a job_load on a pool machine, where per-member
    load placement is unmodelled (# PENDING: Qr3) -- resource semaphores on
    such a job still apply. Cross-machine shared locks need no separate refusal:
    the DL-49 foreign-machine ERROR already makes every runnable job local, so
    every honored resource is contended on this one host (revisit if distributed
    execution lands, DL-49 future track)."""
    items: list[PreflightItem] = []

    def err(message: str) -> None:
        items.append(PreflightItem(severity="ERROR", code="resources", job=name, message=message))

    for accessor in (job.job_load_units, job.priority_value):
        try:
            accessor()  # the accessor's ValueError names the offending attribute
        except ValueError as exc:
            err(str(exc))
    spec = job.exec_
    if spec is not None and spec.machine is not None:
        machine = catalog.machines.get(spec.machine)
        if machine is not None:
            try:
                machine.max_load_units()
            except ValueError as exc:
                err(f"machine {spec.machine!r}: {exc}")
            try:
                load = job.job_load_units()
            except ValueError:
                load = None  # already reported above
            if machine.members and load:
                items.append(
                    PreflightItem(
                        severity="WARN",
                        code="resources",
                        job=name,
                        message=f"job_load={load} on pool machine {spec.machine!r}:"
                        " machine-load throttle unmodelled (PENDING Qr3);"
                        " resource semaphores still apply",
                    )
                )
    seen: set[str] = set()
    for ref in job.resources:
        if ref.name in seen:
            err(f"resources: {ref.name!r} requested more than once -- ambiguous demand (DL-50)")
        seen.add(ref.name)
        resource = catalog.resources.get(ref.name)
        if resource is None:
            err(
                f"resources: {ref.name!r} has no insert_resource in the set --"
                " cannot size the semaphore (DL-50; supply the definition"
                " or drop the requirement)"
            )
            continue
        try:
            capacity = resource.capacity_units()
        except ValueError as exc:
            err(f"resource {ref.name!r}: {exc}")
            continue
        if capacity is None:
            err(f"resource {ref.name!r} has no `amount` -- cannot size the semaphore (DL-50)")
            continue
        if ref.quantity > capacity:
            err(
                f"resources: {ref.name!r} QUANTITY={ref.quantity} exceeds its amount={capacity}"
                " -- can never be satisfied, the job would hang in QUE_WAIT forever (DL-50)"
            )
        res_type = (resource.res_type or "").strip().upper()
        if res_type not in ("", "R", "D", "T"):
            err(
                f"resource {ref.name!r} res_type {resource.res_type!r} is not R/D/T --"
                " unknown release semantics (DL-50)"
            )
    return items


def _preflight_local_day(
    tz_name: str | None, start: datetime, aliases: Mapping[str, str] | None = None
) -> date:
    """The run anchor as the job's LOCAL day (E10 basis; unresolvable
    zones fall back to the naive basis -- they carry their own ERROR)."""
    tz = None
    if tz_name and (resolved := resolve_timezone(tz_name, aliases)) is not None:
        tz = resolved.tz
    return (start.replace(tzinfo=UTC).astimezone(tz) if tz else start).date()


def _next_eligible_day(
    run: frozenset[date] | CompiledCalendar,
    exclude: frozenset[date] | CompiledCalendar | None,
    anchor: date,
) -> date | None:
    """Earliest run-source day at or after `anchor` surviving exclusion
    (DL-57 preflight probe). Two years of consecutive exclusions reads as
    never-fires -- a probe bound, not a semantics claim."""

    def excluded(day: date) -> bool:
        if exclude is None:
            return False
        if isinstance(exclude, CompiledCalendar):
            return day in exclude.days_between(day, day)
        return day in exclude

    if isinstance(run, frozenset):
        for day in sorted(run):
            if day >= anchor and not excluded(day):
                return day
        return None
    cur = anchor
    for _ in range(732):
        hit = run.first_on_or_after(cur)
        if hit is None:
            return None
        if not excluded(hit):
            return hit
        cur = hit + timedelta(days=1)
    return None


def preflight(
    catalog: CatalogIR,
    *,
    execution: bool = True,
    machine_policy: MachinePolicy = "strict",
    as_machine: frozenset[str] = frozenset(),
    start: datetime | None = None,
    tz_aliases: Mapping[str, str] | None = None,
) -> list[PreflightItem]:
    """ss8: refuse loudly, run honestly. `execution=False` (rehearse) skips
    the machine/owner identity rules -- they guard real processes, and the
    FakeAdapter runs none -- while everything the scheduler and oracle
    depend on (calendars, timezones, construction) still gates.

    `start` (DL-56) is the run/rehearse anchor (naive UTC, the engine
    basis); its only consumer is the calendar-exhaustion WARN -- a
    run_calendar whose last eligible day lies before it never fires. None
    skips that check (the base-zone --timezone flag is NOT consulted here:
    the comparison uses the per-job zone else UTC, advisory only).

    `tz_aliases` (DL-62) is the instance's ujo_timezones table (parsed
    `autotimezone -l` output, --timezone-map). SEM-35 names resolve through
    resolve_timezone's ladder; a unique-city default resolution WARNs, an
    unresolvable name ERRORs with the applicable remedy.

    `as_machine` (DL-52) is the identity this runner answers to. Empty = the
    forward hostname (zero-config, no reverse-DNS); non-empty = EXACTLY those
    names plus localhost (the operator declared it -- no hostname guessing). A
    job whose `machine:` is (or resolves through insert_machine to) an identity
    name runs here; anything else is refused foreign.

    `machine_policy` (DL-49) governs the one ambiguous machine verdict: a
    virtual pool with SOME members on this host and some elsewhere. `strict`
    (default) refuses it -- placement among members is unmodelled;
    `local-eligible` runs it here with a WARN that pool placement was
    ignored. Unambiguous verdicts (all-local, none-local, bad definition)
    ignore the policy."""
    items: list[PreflightItem] = []
    local = _local_identity(as_machine)
    user = getpass.getuser()
    for name, job in sorted(catalog.jobs.items()):
        if job.job_type not in _RUNNABLE_TYPES:
            items.append(
                PreflightItem(
                    severity="ERROR",
                    code="job-type",
                    job=name,
                    message=f"job_type {job.job_type!r} has no adapter"
                    " (runner universe is CMD/BOX/FW)",
                )
            )
        spec = job.exec_
        if execution and spec is not None and spec.machine is not None:
            resolution = resolve_machine(spec.machine, catalog.machines, local)
            if resolution.verdict == "foreign":
                items.append(
                    PreflightItem(
                        severity="ERROR",
                        code="machine",
                        job=name,
                        message=f"machine {spec.machine!r} resolves to {resolution.detail},"
                        f" not this host (accepted: {', '.join(sorted(local))});"
                        " no remote fabric (ss12)",
                    )
                )
            elif resolution.verdict == "error":
                items.append(
                    PreflightItem(
                        severity="ERROR", code="machine", job=name, message=resolution.detail
                    )
                )
            elif resolution.verdict == "mixed":
                if machine_policy == "local-eligible":
                    items.append(
                        PreflightItem(
                            severity="WARN",
                            code="machine-mixed",
                            job=name,
                            message=f"{resolution.detail}: running here"
                            " (--machine-policy local-eligible); pool placement ignored",
                        )
                    )
                else:
                    items.append(
                        PreflightItem(
                            severity="ERROR",
                            code="machine",
                            job=name,
                            message=f"{resolution.detail}; refusing"
                            " (--machine-policy local-eligible to run it here)",
                        )
                    )
        if execution and spec is not None and spec.owner is not None and spec.owner != user:
            items.append(
                PreflightItem(
                    severity="ERROR",
                    code="owner",
                    job=name,
                    message=f"owner {spec.owner!r} is not the invoking user {user!r}"
                    " (no setuid in MVP, ss6)",
                )
            )
        sched = job.schedule
        if sched is not None:
            resolved: dict[str, frozenset[date] | CompiledCalendar] = {}
            for role, ref in (
                ("run_calendar", sched.run_calendar),
                ("exclude_calendar", sched.exclude_calendar),
            ):
                if ref is None:
                    continue
                calendar = catalog.calendars.get(ref)
                if calendar is None:
                    # L018's lint WARN, fail-closed at run (same strictness
                    # split as L016-vs-DL-50 resources)
                    message = (
                        f"{role} {ref!r} has no calendar definition in the loaded set"
                        " -- a missing autocal export cannot be guessed (DL-56)"
                    )
                else:
                    # extended calendars compile through the autocal
                    # interpreter (DL-57); what the SEM-36..39 freeze cannot
                    # express refuses here with the interpreter's reason
                    try:
                        resolved[role] = (
                            compile_calendar(calendar, catalog)
                            if calendar.kind == "extended"
                            else standard_days(calendar)
                        )
                        continue
                    except CalendarRuleError as exc:
                        message = f"{role} {ref!r}: {exc}"
                items.append(
                    PreflightItem(severity="ERROR", code="calendar", job=name, message=message)
                )
            if sched.run_calendar is not None and "run_calendar" in resolved:
                run_src = resolved["run_calendar"]
                exclude_src = resolved.get("exclude_calendar")
                if isinstance(run_src, frozenset) and not isinstance(exclude_src, CompiledCalendar):
                    eligible = run_src - (exclude_src or frozenset())
                    if not eligible:
                        items.append(
                            PreflightItem(
                                severity="WARN",
                                code="calendar",
                                job=name,
                                message=f"run_calendar {sched.run_calendar!r} has no eligible"
                                " dates after exclude_calendar subtraction -- the job never"
                                " fires (DL-56)",
                            )
                        )
                    elif start is not None:
                        local_day = _preflight_local_day(sched.timezone, start, tz_aliases)
                        if max(eligible) < local_day:
                            items.append(
                                PreflightItem(
                                    severity="WARN",
                                    code="calendar",
                                    job=name,
                                    message=f"run_calendar {sched.run_calendar!r} is exhausted:"
                                    f" last eligible date {max(eligible).isoformat()} lies before"
                                    f" the run start -- the job never fires (DL-56)",
                                )
                            )
                elif start is not None:
                    # an extended source probes its generator from the run
                    # anchor (run: wall-now; rehearse: --start); anchorless
                    # construction gets compile validation only (DL-56/57)
                    local_day = _preflight_local_day(sched.timezone, start, tz_aliases)
                    try:
                        nxt = _next_eligible_day(run_src, exclude_src, local_day)
                    except CalendarRuleError as exc:
                        # the interpreter's message names the offending
                        # calendar itself (run or exclude)
                        items.append(
                            PreflightItem(
                                severity="ERROR", code="calendar", job=name, message=str(exc)
                            )
                        )
                    else:
                        if nxt is None:
                            items.append(
                                PreflightItem(
                                    severity="WARN",
                                    code="calendar",
                                    job=name,
                                    message=f"run_calendar {sched.run_calendar!r} generates no"
                                    " eligible day at or after the run start -- the job is"
                                    " dormant (DL-57)",
                                )
                            )
            exclude_only = resolved.get("exclude_calendar")
            if (
                sched.run_calendar is None
                and start is not None
                and (sched.start_times or sched.start_mins)
                and isinstance(exclude_only, CompiledCalendar)
            ):
                # a days_of_week source under an extended exclusion: a
                # standard exclude set can never cover a weekly source, an
                # extended one can (DL-57) -- probe two years of it
                local_day = _preflight_local_day(sched.timezone, start, tz_aliases)
                tokens = (
                    frozenset(_DAY_CODES)
                    if (sched.days_of_week is None or "all" in sched.days_of_week)
                    else frozenset(sched.days_of_week)
                )
                try:
                    covered = exclude_only.days_between(local_day, local_day + timedelta(days=731))
                except CalendarRuleError as exc:
                    items.append(
                        PreflightItem(severity="ERROR", code="calendar", job=name, message=str(exc))
                    )
                else:
                    if all(
                        _DAY_CODES[day.weekday()] not in tokens or day in covered
                        for day in (local_day + timedelta(days=i) for i in range(732))
                    ):
                        items.append(
                            PreflightItem(
                                severity="WARN",
                                code="calendar",
                                job=name,
                                message=f"exclude_calendar {sched.exclude_calendar!r} covers"
                                " every eligible day within two years of the run start --"
                                " the job never fires (DL-57 probe bound)",
                            )
                        )
        if sched is not None and sched.timezone is not None:
            tz_res = resolve_timezone(sched.timezone, tz_aliases)
            if tz_res is None:
                if tz_aliases is not None:
                    detail = "not a zoneinfo name and not in the supplied --timezone-map"
                elif len(candidates := _city_candidates(sched.timezone)) > 1:
                    detail = (
                        f"ambiguous city name ({', '.join(candidates)}); use the full"
                        " zone name or a --timezone-map"
                    )
                else:
                    detail = (
                        "not a zoneinfo name; the vendor resolves SEM-35 names through"
                        " the instance's ujo_timezones table -- feed its `autotimezone"
                        " -l` listing in via --timezone-map"
                    )
                items.append(
                    PreflightItem(
                        severity="ERROR",
                        code="timezone",
                        job=name,
                        message=f"timezone {sched.timezone!r} is not resolvable: {detail}",
                    )
                )
            elif tz_res.how == "city":
                items.append(
                    PreflightItem(
                        severity="WARN",
                        code="timezone",
                        job=name,
                        message=f"timezone {sched.timezone!r} assumed to be {tz_res.zone}"
                        " (unique zoneinfo city match, DL-62); pin it with --timezone-map"
                        " (`autotimezone -l`) if the estate maps it differently",
                    )
                )
            elif tz_res.how == "posix" and tz_res.tz.utcoffset(None) != timedelta(0):
                offset = tz_res.tz.utcoffset(None) or timedelta(0)
                hh, rem = divmod(abs(int(offset.total_seconds())), 3600)
                rendered = f"UTC{'+' if offset >= timedelta(0) else '-'}{hh:02d}:{rem // 60:02d}"
                items.append(
                    PreflightItem(
                        severity="WARN",
                        code="timezone",
                        job=name,
                        message=f"timezone {sched.timezone!r} read as a POSIX fixed offset"
                        f" = {rendered} (POSIX offsets are west-positive: GMT+5 means"
                        " 5h WEST of GMT)",
                    )
                )
        if job.sem.n_retrys > 0:
            items.append(
                PreflightItem(
                    severity="WARN",
                    code="n-retrys",
                    job=name,
                    message=f"n_retrys={job.sem.n_retrys}: runs WITHOUT retries (unmodeled v1,"
                    " DL-53 scope; a shell-side retry would fork semantics from the oracle)",
                )
            )
        items.extend(_resource_preflight(name, job, catalog))
    try:
        Oracle(catalog)
    except OracleError as exc:
        items.append(
            PreflightItem(
                severity="ERROR",
                code="oracle",
                message=f"oracle construction failed: {exc}",
            )
        )
    try:
        graphlib.TopologicalSorter(and_success_skeleton(catalog)).prepare()
    except graphlib.CycleError as exc:
        items.append(
            PreflightItem(
                severity="WARN",
                code="skeleton-cycle",
                message="cycle in the AND-success skeleton"
                f" ({' -> '.join(exc.args[1])}): legal AutoSys (edge-triggered re-runs,"
                " DL-13/L010); `plan` is disabled for this estate",
            )
        )
    return items
