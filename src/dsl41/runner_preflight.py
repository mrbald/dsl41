"""Runner preflight (ss8): refuse loudly, run honestly (ss5, ss10; DL-45, DL-74).

ERROR refuses the run (job-type, machine, owner, execution-input, calendar, timezone, oracle
construction). WARN prints, journals and runs (n-retrys DL-53, resources, exhausted run_calendar
DL-56, AND-success skeleton cycle). A cycle is legal AutoSys (DL-13/L010) and only disables
`plan`. The identity rules (machine, owner) and the execution-input rule (envvars, global
substitution, chk_files; DL-240) guard real execution and are skipped for rehearse
(`execution=False`), where the FakeAdapter runs nothing.
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
from dsl41.capacity import RES_TYPES, resource_type
from dsl41.conditions import And, Cond, Paren, StatusAtom
from dsl41.ir import CatalogIR, ExecSpec, JobIR, MachineIR, unquote_jil_value
from dsl41.oracle import Oracle
from dsl41.runner_config import MachinePolicy as _MachinePolicy
from dsl41.oracle_state import OracleError
from dsl41.runner_scheduler import _DAY_CODES
from dsl41.semantics import SemanticSwitches
from dsl41.timezones import city_candidates, resolve_timezone, to_local


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
    """The machine names this runner answers to (DL-52): `localhost`, plus the declared
    identities (`--as-machine`) when given. With none declared it uses the forward hostname
    (short and full) and never reverse DNS, which can stall and names a different namespace from
    the estate's machines."""
    identity = {"localhost"}
    if declared:
        identity |= {d.strip().lower() for d in declared if d.strip()}
        return frozenset(identity)
    hostname = socket_mod.gethostname().lower()
    identity |= {hostname, hostname.split(".")[0]}
    return frozenset(identity)


#: Machine `type:` values the resolver understands (DL-49); anything else is refused.
_KNOWN_MACHINE_TYPES = frozenset({"a", "r", "n", "v"})

MachineVerdict = Literal["local", "foreign", "mixed", "error"]

#: Re-exported: `period.RuntimeProfile.machine_policy` is the owner (DL-209).
MachinePolicy = _MachinePolicy


@dataclass(frozen=True)
class MachineResolution:
    """Outcome of resolving a job's `machine:` through `insert_machine` (DL-49). `detail` is the
    resolved host (local, foreign), the reason (error) or the pool summary (mixed)."""

    verdict: MachineVerdict
    detail: str


def _node_name(machine: MachineIR) -> str | None:
    """The machine's node_name, unquoted: it feeds host resolution, so it is not the raw
    opaque carry."""
    raw = machine.attrs.get("node_name")
    return unquote_jil_value(raw) if raw else None


def _leaf_host(machine: MachineIR) -> tuple[str | None, str | None]:
    """Effective host of a non-virtual machine: (host, error). An agent must carry node_name;
    a real machine falls back to its record name; a missing or unknown type is refused."""
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
    """Resolve a job's `machine:` name to a placement verdict against the declared identity
    `local` (DL-52), honouring `insert_machine` (DL-49).

    A direct name match wins over the record's node_name. A virtual machine is local iff all
    members resolve local, foreign iff none, else mixed. An undefined name is foreign. A bad
    definition (missing type; undefined, nested or typeless member; empty pool) is `error`."""
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
    """job -> success-predecessors reachable through AND/Paren spines only (an s() atom under an
    OR is an alternative). Instance-qualified and undefined references are skipped. The ss8 cycle
    WARN and the ss10 `plan` view share it, so they agree on acyclicity."""

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
    """DL-50: the oracle honors resource and load attributes; preflight refuses, in run and
    rehearse alike, the shapes it cannot model:
      * a `resources:` requirement whose resource is unsized (no insert_resource, or no
        parseable `amount`), stricter than L016's warn;
      * an unknown res_type (not R/D/T);
      * FREE=Y or FREE=A on a depletable; FREE is renewable-only (DL-287, Q12);
      * a QUANTITY above the resource's `amount`: never satisfiable, and at a positive priority
        it blocks every lower priority naming the resource (DL-255);
      * a malformed job_load, priority or max_load;
      * a `job_load` above its machine's `max_load` at a positive priority: it never fits and
        blocks every lower priority on that machine (DL-247). Priority 0 or unset is exempt.
    It WARNs on a job_load on a pool machine, where per-member load placement is unmodelled
    (# PENDING: Qr3); resource semaphores still apply. Cross-machine locks need no refusal: the
    DL-49 foreign-machine ERROR makes every runnable job local."""
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
                max_load = machine.max_load_units()
            except ValueError as exc:
                err(f"machine {spec.machine!r}: {exc}")
                max_load = None
            try:
                load = job.job_load_units()
                priority = job.priority_value()
            except ValueError:
                load = priority = None  # already reported above
            if (
                not machine.members
                and load is not None
                and priority is not None
                and priority > 0
                and max_load is not None
                and load > max_load
            ):
                err(
                    f"job_load={load} exceeds machine {spec.machine!r} max_load={max_load}"
                    f" at priority={priority} -- can never fit, the job would hang in"
                    " QUE_WAIT forever and block every lower priority there (DL-247)"
                )
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
                " and, at a positive priority, block every lower priority naming it (DL-255)"
            )
        res_type = resource_type(resource)
        if res_type and res_type not in RES_TYPES:
            err(
                f"resource {ref.name!r} res_type {resource.res_type!r} is not R/D/T --"
                " unknown release semantics (DL-50)"
            )
        if res_type == "D" and ref.free in ("Y", "A"):
            err(
                f"resource {ref.name!r} is depletable and FREE={ref.free} is undocumented there:"
                ' "Optional for renewable virtual resources only" ("resources Attribute",'
                " AutoSys 24.2) -- unknown release semantics (DL-50, DL-287, Q12); drop FREE"
                " or write FREE=N, or retype the resource R if it is renewable"
            )
    return items


def _preflight_local_day(
    tz_name: str | None,
    start: datetime,
    aliases: Mapping[str, str] | None = None,
    default_tz: str | None = None,
) -> date:
    """The run anchor as the job's local day, on the scheduler's ladder (E10 basis; DL-212): the
    job's zone, else the run's base zone, else the naive basis. An unresolvable zone falls
    through, because it carries its own ERROR."""
    tz = None
    for name in (tz_name, default_tz):
        if name and (resolved := resolve_timezone(name, aliases)) is not None:
            tz = resolved.tz
            break
    return to_local(start, tz).date()


def _next_eligible_day(
    run: frozenset[date] | CompiledCalendar,
    exclude: frozenset[date] | CompiledCalendar | None,
    anchor: date,
) -> date | None:
    """Earliest run-source day at or after `anchor` surviving exclusion (DL-57). Two years of
    exclusions reads as never-fires: a probe bound, not a semantics claim."""

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


def _job_type_preflight(name: str, job: JobIR) -> list[PreflightItem]:
    """ERROR: job_type is outside the executable universe (CMD/BOX/FW)."""
    items: list[PreflightItem] = []

    def err(message: str) -> None:
        items.append(PreflightItem(severity="ERROR", code="job-type", job=name, message=message))

    if job.job_type not in _RUNNABLE_TYPES:
        err(f"job_type {job.job_type!r} has no adapter (runner universe is CMD/BOX/FW)")
    return items


def _machine_preflight(
    name: str,
    job: JobIR,
    catalog: CatalogIR,
    local: frozenset[str],
    machine_policy: MachinePolicy,
) -> list[PreflightItem]:
    """ERROR/WARN: `machine:` placement (DL-49/DL-52). Skipped for rehearse."""
    items: list[PreflightItem] = []

    def err(message: str) -> None:
        items.append(PreflightItem(severity="ERROR", code="machine", job=name, message=message))

    def warn(message: str) -> None:
        items.append(
            PreflightItem(severity="WARN", code="machine-mixed", job=name, message=message)
        )

    spec = job.exec_
    if spec is None or spec.machine is None:
        return items
    resolution = resolve_machine(spec.machine, catalog.machines, local)
    if resolution.verdict == "foreign":
        err(
            f"machine {spec.machine!r} resolves to {resolution.detail},"
            f" not this host (accepted: {', '.join(sorted(local))}); no remote fabric (ss12)"
        )
    elif resolution.verdict == "error":
        err(resolution.detail)
    elif resolution.verdict == "mixed":
        if machine_policy == "local-eligible":
            warn(
                f"{resolution.detail}: running here"
                " (--machine-policy local-eligible); pool placement ignored"
            )
        else:
            err(f"{resolution.detail}; refusing (--machine-policy local-eligible to run it here)")
    return items


def _owner_preflight(name: str, job: JobIR, user: str) -> list[PreflightItem]:
    """ERROR: `exec_.owner` set and not the invoking user (no setuid, ss6). Skipped for
    rehearse."""
    items: list[PreflightItem] = []

    def err(message: str) -> None:
        items.append(PreflightItem(severity="ERROR", code="owner", job=name, message=message))

    spec = job.exec_
    if spec is not None and spec.owner is not None and spec.owner != user:
        err(f"owner {spec.owner!r} is not the invoking user {user!r} (no setuid in MVP, ss6)")
    return items


def _execution_input_preflight(name: str, job: JobIR) -> list[PreflightItem]:
    """ERROR: three execution inputs real execution never applies: `envvars` (CMD), a `$$NAME`
    global-substitution site on an exec_ field, and `chk_files` (DL-240). BOX jobs are exempt
    (SEM-10). Skipped for rehearse."""
    items: list[PreflightItem] = []
    if job.job_type == "BOX":
        return items

    # one closure per code: the register's scanner reads the constant `code=`
    # on each PreflightItem call to enumerate the preflight_code surface

    def err_envvars(message: str) -> None:
        items.append(PreflightItem(severity="ERROR", code="envvars", job=name, message=message))

    def err_global_sub(message: str) -> None:
        items.append(
            PreflightItem(severity="ERROR", code="global-substitution", job=name, message=message)
        )

    def err_chk_files(message: str) -> None:
        items.append(PreflightItem(severity="ERROR", code="chk-files", job=name, message=message))

    spec = job.exec_
    if isinstance(spec, ExecSpec) and spec.envvars is not None:
        # the value is not echoed: an environment list may carry a secret
        err_envvars(
            "envvars is carried but never applied to the child environment;"
            " real execution is refused (DL-240)"
        )

    if spec is not None:
        exec_attrs = set(type(spec).model_fields) - {"kind"}
        seen: set[tuple[str, str]] = set()
        for site in job.var_sites:
            if site.attr not in exec_attrs:
                continue
            key = (site.attr, site.name)
            if key in seen:
                continue
            seen.add(key)
            err_global_sub(
                f"{site.attr} references global $${site.name}; global substitution"
                " is not implemented, so the literal text would be used; real"
                " execution is refused (DL-240)"
            )

    # passthrough is keyed by the raw attribute case (ir._lookup_ci, lint.py's
    # own TIME_CLUSTER scan), so CHK_FILES must match too -- case-insensitively
    chk_files = next((v for k, v in job.passthrough.items() if k.lower() == "chk_files"), None)
    if chk_files is not None:
        err_chk_files(
            f"chk_files {chk_files!r} is carried but the pre-start disk-space gate"
            " is never evaluated; real execution is refused (DL-240)"
        )
    return items


def _calendar_preflight(
    name: str,
    job: JobIR,
    catalog: CatalogIR,
    start: datetime,
    tz_aliases: Mapping[str, str] | None,
    default_tz: str | None,
    semantics: SemanticSwitches | None,
) -> list[PreflightItem]:
    """ERROR/WARN: `run_calendar`/`exclude_calendar` resolution, exhaustion and dormancy
    (DL-56/DL-57); every finding carries `code="calendar"`."""
    items: list[PreflightItem] = []

    def err(message: str) -> None:
        items.append(PreflightItem(severity="ERROR", code="calendar", job=name, message=message))

    def warn(message: str) -> None:
        items.append(PreflightItem(severity="WARN", code="calendar", job=name, message=message))

    sched = job.schedule
    if sched is None:
        return items
    resolved: dict[str, frozenset[date] | CompiledCalendar] = {}
    for role, ref in (
        ("run_calendar", sched.run_calendar),
        ("exclude_calendar", sched.exclude_calendar),
    ):
        if ref is None:
            continue
        calendar = catalog.calendars.get(ref)
        if calendar is None:
            # L018's lint WARN is an ERROR at run, as L016 is for resources (DL-50)
            err(
                f"{role} {ref!r} has no calendar definition in the loaded set"
                " -- a missing autocal export cannot be guessed (DL-56)"
            )
            continue
        # extended calendars compile through autocal (DL-57); what SEM-36..39 cannot express
        # refuses with the interpreter's reason
        try:
            resolved[role] = (
                compile_calendar(calendar, catalog, semantics)
                if calendar.kind == "extended"
                else standard_days(calendar)
            )
        except CalendarRuleError as exc:
            err(f"{role} {ref!r}: {exc}")
    if sched.run_calendar is not None and "run_calendar" in resolved:
        run_src = resolved["run_calendar"]
        exclude_src = resolved.get("exclude_calendar")
        if isinstance(run_src, frozenset) and not isinstance(exclude_src, CompiledCalendar):
            eligible = run_src - (exclude_src or frozenset())
            if not eligible:
                warn(
                    f"run_calendar {sched.run_calendar!r} has no eligible"
                    " dates after exclude_calendar subtraction -- the job never"
                    " fires (DL-56)"
                )
            else:
                local_day = _preflight_local_day(sched.timezone, start, tz_aliases, default_tz)
                if max(eligible) < local_day:
                    warn(
                        f"run_calendar {sched.run_calendar!r} is exhausted:"
                        f" last eligible date {max(eligible).isoformat()} lies before"
                        f" the run start -- the job never fires (DL-56)"
                    )
        else:
            # an extended source probes its generator from the run anchor (DL-213)
            local_day = _preflight_local_day(sched.timezone, start, tz_aliases, default_tz)
            try:
                nxt = _next_eligible_day(run_src, exclude_src, local_day)
            except CalendarRuleError as exc:
                # the message names the offending calendar
                err(str(exc))
            else:
                if nxt is None:
                    warn(
                        f"run_calendar {sched.run_calendar!r} generates no"
                        " eligible day at or after the run start -- the job is"
                        " dormant (DL-57)"
                    )
    exclude_only = resolved.get("exclude_calendar")
    if (
        sched.run_calendar is None
        and (sched.start_times or sched.start_mins)
        and isinstance(exclude_only, CompiledCalendar)
    ):
        # a standard exclude set cannot cover a weekly source, an extended one can (DL-57)
        local_day = _preflight_local_day(sched.timezone, start, tz_aliases, default_tz)
        tokens = (
            frozenset(_DAY_CODES)
            if (sched.days_of_week is None or "all" in sched.days_of_week)
            else frozenset(sched.days_of_week)
        )
        try:
            covered = exclude_only.days_between(local_day, local_day + timedelta(days=731))
        except CalendarRuleError as exc:
            err(str(exc))
        else:
            if all(
                _DAY_CODES[day.weekday()] not in tokens or day in covered
                for day in (local_day + timedelta(days=i) for i in range(732))
            ):
                warn(
                    f"exclude_calendar {sched.exclude_calendar!r} covers"
                    " every eligible day within two years of the run start --"
                    " the job never fires (DL-57 probe bound)"
                )
    return items


def _timezone_preflight(
    name: str, job: JobIR, tz_aliases: Mapping[str, str] | None
) -> list[PreflightItem]:
    """ERROR/WARN: `timezone:` resolution (SEM-35/DL-62)."""
    items: list[PreflightItem] = []

    def err(message: str) -> None:
        items.append(PreflightItem(severity="ERROR", code="timezone", job=name, message=message))

    def warn(message: str) -> None:
        items.append(PreflightItem(severity="WARN", code="timezone", job=name, message=message))

    sched = job.schedule
    if sched is None or sched.timezone is None:
        return items
    tz_res = resolve_timezone(sched.timezone, tz_aliases)
    if tz_res is None:
        if tz_aliases is not None:
            detail = "not a zoneinfo name and not in the supplied --timezone-map"
        elif len(candidates := city_candidates(sched.timezone)) > 1:
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
        err(f"timezone {sched.timezone!r} is not resolvable: {detail}")
    elif tz_res.how == "city":
        warn(
            f"timezone {sched.timezone!r} assumed to be {tz_res.zone}"
            " (unique zoneinfo city match, DL-62); pin it with --timezone-map"
            " (`autotimezone -l`) if the estate maps it differently"
        )
    elif tz_res.how == "posix" and tz_res.tz.utcoffset(None) != timedelta(0):
        offset = tz_res.tz.utcoffset(None) or timedelta(0)
        hh, rem = divmod(abs(int(offset.total_seconds())), 3600)
        rendered = f"UTC{'+' if offset >= timedelta(0) else '-'}{hh:02d}:{rem // 60:02d}"
        warn(
            f"timezone {sched.timezone!r} read as a POSIX fixed offset"
            f" = {rendered} (POSIX offsets are west-positive: GMT+5 means"
            " 5h WEST of GMT)"
        )
    return items


def _retry_preflight(name: str, job: JobIR) -> list[PreflightItem]:
    """WARN: `n_retrys > 0` runs without retries (DL-53)."""
    items: list[PreflightItem] = []

    def warn(message: str) -> None:
        items.append(PreflightItem(severity="WARN", code="n-retrys", job=name, message=message))

    if job.sem.n_retrys > 0:
        warn(
            f"n_retrys={job.sem.n_retrys}: runs WITHOUT retries (unmodeled v1,"
            " DL-53 scope; a shell-side retry would fork semantics from the oracle)"
        )
    return items


def _oracle_preflight(
    catalog: CatalogIR, semantics: SemanticSwitches | None
) -> list[PreflightItem]:
    """ERROR: the oracle itself fails to construct over this catalog."""
    items: list[PreflightItem] = []

    def err(message: str) -> None:
        items.append(PreflightItem(severity="ERROR", code="oracle", message=message))

    try:
        Oracle(catalog, semantics=semantics)
    except OracleError as exc:
        err(f"oracle construction failed: {exc}")
    return items


def _skeleton_cycle_preflight(catalog: CatalogIR) -> list[PreflightItem]:
    """WARN: a cycle in the AND-success skeleton disables `plan` (legal AutoSys, DL-13/L010)."""
    items: list[PreflightItem] = []

    def warn(message: str) -> None:
        items.append(PreflightItem(severity="WARN", code="skeleton-cycle", message=message))

    try:
        graphlib.TopologicalSorter(and_success_skeleton(catalog)).prepare()
    except graphlib.CycleError as exc:
        warn(
            "cycle in the AND-success skeleton"
            f" ({' -> '.join(exc.args[1])}): legal AutoSys (edge-triggered re-runs,"
            " DL-13/L010); `plan` is disabled for this estate"
        )
    return items


def preflight(
    catalog: CatalogIR,
    *,
    execution: bool = True,
    machine_policy: MachinePolicy = "strict",
    as_machine: frozenset[str] = frozenset(),
    start: datetime | None = None,
    default_tz: str | None = None,
    tz_aliases: Mapping[str, str] | None = None,
    semantics: SemanticSwitches | None = None,
) -> list[PreflightItem]:
    """ss8: refuse loudly, run honestly. `execution=False` (rehearse) skips the machine, owner
    and execution-input rules; calendars, timezones and construction still gate.

    `start` (DL-56) is the run anchor (naive UTC) for the calendar exhaustion and dormancy
    probes; omitted, it is now (DL-213). `default_tz` (DL-212) is the run's base zone: the probes
    read the anchor in the job's zone, else this one, else UTC. `tz_aliases` (DL-62) is the
    instance's ujo_timezones table (`--timezone-map`): a unique-city resolution WARNs, an
    unresolvable name ERRORs. `semantics` are the run's switches, applied when compiling extended
    calendars; None is the registry defaults.

    `as_machine` (DL-52) is the declared identity: empty means the forward hostname, otherwise
    exactly those names plus localhost. `machine_policy` (DL-49) settles a virtual pool with
    some members here: `strict` refuses it, `local-eligible` runs it here with a WARN. The other
    verdicts ignore it.

    The per-job rules are independent and run in a fixed order (DL-178t)."""
    if start is None:
        # no anchor means now (DL-213)
        start = datetime.now(UTC).replace(tzinfo=None)
    items: list[PreflightItem] = []
    local = _local_identity(as_machine)
    user = getpass.getuser()
    for name, job in sorted(catalog.jobs.items()):
        items.extend(_job_type_preflight(name, job))
        if execution:
            items.extend(_machine_preflight(name, job, catalog, local, machine_policy))
            items.extend(_owner_preflight(name, job, user))
            items.extend(_execution_input_preflight(name, job))
        items.extend(
            _calendar_preflight(name, job, catalog, start, tz_aliases, default_tz, semantics)
        )
        items.extend(_timezone_preflight(name, job, tz_aliases))
        items.extend(_retry_preflight(name, job))
        items.extend(_resource_preflight(name, job, catalog))
    items.extend(_oracle_preflight(catalog, semantics))
    items.extend(_skeleton_cycle_preflight(catalog))
    return items
