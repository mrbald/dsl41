"""The simulation coverage register's rows, as literal data (DL-209).

Every row is a plain mapping validated into `simulation_register.Behaviour`
at import. The members are LITERAL here on purpose: deriving them from the
inventories would make the register agree with the code by construction and
catch nothing. The test derives the domains instead and fails on a member
with no row -- and on a row whose member the code no longer has.

Fixture strings are interpreted by `tests/test_simulation_register.py`
according to the surface's fixture kind (jil, cond, scenario, profile,
outcome) -- one kind per surface, the test's own table. A row does not say
its kind (DL-75 review 2026-09-19).

This module imports nothing from `simulation_register`: the data is plain,
so the dependency runs one way and the two files never form a cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

Row = dict[str, object]

SUPPORTED = "supported"
PROVISIONAL = "provisional"
REFUSED = "refused"
PASSTHROUGH = "passthrough"

#: Every open question the calendar sandbox protocol discriminates at once.
_CAL_PROTOCOL = "Q8b / Q8c / Q8d"


# ------------------------------------------------------------------ fixtures

MACHINE_BLOCK = "insert_machine: M0\ntype: a\nnode_name: localhost"
JOB_BLOCK = "insert_job: J0\njob_type: c\ncommand: true\nmachine: M0"
#: the same job with a machine-load demand, so a pool WARN has something to fire on
LOADED_JOB_BLOCK = JOB_BLOCK + "\njob_load: 1"
RESOURCE_BLOCK = "insert_resource: R0\nres_type: R\namount: 4"
BOX_BLOCK = "insert_job: BOX0\njob_type: b"
CYCLE_NAME = "CY0"
CYCLE_BLOCK = f"cycle: {CYCLE_NAME}\nstart_date: 01/01/2026\nend_date: 03/31/2026"
HOLCAL_NAME = "HOLS"
HOLCAL_BLOCK = f"calendar: {HOLCAL_NAME}\n01/01/2026"


def _nested(rule: str, depth: int) -> str:
    """`rule` wrapped in `depth` parenthesis pairs."""
    return "(" * depth + rule + ")" * depth


def _weekly_dates(first: str, count: int) -> str:
    """`count` consecutive weekly date rows from `first` (mm/dd/yyyy)."""
    day = datetime.strptime(first, "%m/%d/%Y").date()
    return "\n".join((day + timedelta(days=7 * week)).strftime("%m/%d/%Y") for week in range(count))


#: A holiday calendar that blanks every workday for longer than the 366-day
#: walk cap: 60 consecutive Mondays, against a Monday-only workday mask.
#: Anything shorter lets the walk land, and proves nothing.
STARVED_HOLCAL_BLOCK = f"calendar: {HOLCAL_NAME}\n" + _weekly_dates("01/06/2025", 60)


def _estate(*blocks: str) -> str:
    """One JIL file from statement blocks, blank-line separated."""
    return "\n\n".join(block.strip("\n") for block in blocks) + "\n"


def _job(*extra: str, **attrs: str | None) -> str:
    """The base estate with `attrs` overriding the job's own three; a None
    value drops that attribute. `extra` appends whole statements after it."""
    fields: dict[str, str | None] = {"job_type": "c", "command": "true", "machine": "M0"}
    fields.update(attrs)
    body = "".join(f"{key}: {value}\n" for key, value in fields.items() if value is not None)
    return _estate(MACHINE_BLOCK, "insert_job: J0\n" + body, *extra)


def _machine_estate(kind: str) -> str:
    """The base estate whose machine is of `kind`; a virtual one gets a member."""
    if kind == "v":
        return _estate(
            "insert_machine: M0\ntype: v\nmachine: A1",
            "insert_machine: A1\ntype: a\nnode_name: localhost",
            JOB_BLOCK,
        )
    return _estate(f"insert_machine: M0\ntype: {kind}\nnode_name: localhost", JOB_BLOCK)


def _stmt(text: str) -> str:
    """The base estate plus one more statement."""
    return _estate(MACHINE_BLOCK, JOB_BLOCK, text)


def _cal(*lines: str, cyccal: bool = False, holcal: bool = False) -> str:
    """The base estate plus one extended calendar carrying `lines`.

    `cyccal`/`holcal` add the `cyccal:`/`holcal:` line AND the cycle or
    standard calendar it names. A calendar whose dependency is absent parses
    and lowers but cannot COMPILE, and a scope fixture the engine refuses to
    read proves nothing (the compile gate in the test enforces this)."""
    body = list(lines)
    extra: list[str] = []
    if cyccal:
        body.append(f"cyccal: {CYCLE_NAME}")
        extra.append(CYCLE_BLOCK)
    if holcal:
        body.append(f"holcal: {HOLCAL_NAME}")
        extra.append(HOLCAL_BLOCK)
    calendar = "extended_calendar: EC0\n" + "".join(f"{line}\n" for line in body)
    return _estate(MACHINE_BLOCK, JOB_BLOCK, calendar, *extra)


def _scn(jil: str, *events: str) -> str:
    """A scenario fixture: JIL, then `--`, then one event per line
    (`<minutes> <KIND> [key=value ...]`)."""
    return jil.rstrip("\n") + "\n--\n" + "".join(f"{event}\n" for event in events)


BASE_JIL = _estate(MACHINE_BLOCK, JOB_BLOCK)
#: the quiet estate for rows the base estate itself would trigger
GLOBAL_JIL = "insert_global: G0\nvalue: 0\n"

TICKER_BLOCK = "insert_job: TICK\njob_type: c\ncommand: true\nmachine: M0"
SCHEDULED_JOB = _job(date_conditions="1", days_of_week="all", start_times='"08:00"')


# ------------------------------------------------------------------ builders


def _row(**kw: object) -> Row:
    return dict(kw)


def _rows(
    surface: str,
    members: tuple[str, ...],
    *,
    klass: str,
    cite: str,
    effect: str,
    trigger: Callable[[str], str],
    quiet: str | None = None,
    **extra: object,
) -> tuple[Row, ...]:
    """One ruling over a literal tuple of members, with a templated trigger.
    `{member}` in `cite` and `effect` expands to the member."""
    return tuple(
        _row(
            surface=surface,
            member=member,
            klass=klass,
            cite=cite.format(member=member),
            effect=effect.format(member=member),
            trigger=trigger(member),
            quiet=quiet,
            **extra,
        )
        for member in members
    )


# ------------------------------------------------------------------ statement

_REFUSED_STATEMENTS: tuple[str, ...] = (
    "delete_blob",
    "delete_box",
    "delete_connectionprofile",
    "delete_glob",
    "delete_global",
    "delete_job",
    "delete_job_type",
    "delete_machine",
    "delete_monbro",
    "delete_resource",
    "delete_xinst",
    "insert_blob",
    "insert_connectionprofile",
    "insert_glob",
    "insert_job_type",
    "insert_monbro",
    "override_job",
    "rename_job",
    "update_connectionprofile",
    "update_job",
    "update_job_type",
    "update_machine",
    "update_monbro",
    "update_resource",
    "update_xinst",
)

STATEMENT_ROWS: tuple[Row, ...] = (
    _row(
        surface="statement",
        member="insert_job",
        klass=SUPPORTED,
        cite="DL-29",
        effect="a job definition is lowered whole: linkage, semantics, schedule and exec spec",
        trigger=BASE_JIL,
        quiet=GLOBAL_JIL,
    ),
    _row(
        surface="statement",
        member="insert_machine",
        klass=SUPPORTED,
        cite="DL-49",
        effect="a machine definition is lowered to its type, node_name and pool members",
        trigger=BASE_JIL,
        quiet=GLOBAL_JIL,
    ),
    _row(
        surface="statement",
        member="insert_global",
        klass=SUPPORTED,
        cite="SEM-08",
        effect="a global variable's declared value seeds the oracle's global store",
        trigger=_stmt("insert_global: G0\nvalue: 0"),
    ),
    _row(
        surface="statement",
        member="insert_resource",
        klass=SUPPORTED,
        cite="DL-50",
        effect="a resource definition sizes one capacity bucket",
        trigger=_stmt(RESOURCE_BLOCK),
    ),
    _row(
        surface="statement",
        member="insert_xinst",
        klass=SUPPORTED,
        cite="SEM-07",
        effect="an external-instance definition is carried; cross-instance atoms read it by name",
        trigger=_stmt("insert_xinst: X0\nxtype: a"),
    ),
    _row(
        surface="statement",
        member="calendar",
        klass=SUPPORTED,
        cite="SEM-36, DL-36",
        effect="a standard calendar's date rows become the day set holcal and run_calendar read",
        trigger=_stmt("calendar: SC0\n01/01/2026"),
    ),
    _row(
        surface="statement",
        member="extended_calendar",
        klass=SUPPORTED,
        cite="SEM-36, DL-36",
        effect="an extended calendar's rules compile to a day generator",
        trigger=_stmt("extended_calendar: EC0\ncondition: DAILY"),
    ),
    _row(
        surface="statement",
        member="ext_calendar",
        klass=SUPPORTED,
        cite="SEM-36, DL-60",
        effect="the Manage Calendars spelling of extended_calendar, accepted as input leniency",
        trigger=_stmt("ext_calendar: EC1\ncondition: DAILY"),
    ),
    _row(
        surface="statement",
        member="cycle",
        klass=SUPPORTED,
        cite="SEM-39",
        effect="a cycle's start_date/end_date pairs become the periods cycle-scoped"
        " tokens count in",
        trigger=_stmt("cycle: CY0\nstart_date: 01/01/2026\nend_date: 03/31/2026"),
    ),
) + _rows(
    "statement",
    _REFUSED_STATEMENTS,
    klass=REFUSED,
    cite="ir._Lowerer.run, DL-29",
    effect="lowering refuses {member}: merging and out-of-scope object classes are"
    " semantics this compiler does not model",
    trigger=lambda m: _stmt(f"{m}: X0"),
)


# ------------------------------------------------------------------ job_attr

_ANNOTATION_ATTRS: tuple[str, ...] = (
    "alarm_if_fail",
    "alarm_if_terminated",
    "description",
    "heartbeat_interval",
    "max_run_alarm",
    "min_run_alarm",
    "notification_alarm_types",
    "notification_emailaddress",
    "notification_emailaddress_on_alarm",
    "notification_emailaddress_on_failure",
    "notification_emailaddress_on_success",
    "notification_emailaddress_on_terminated",
    "notification_msg",
    "notification_template",
    "send_notification",
)

#: PASSTHROUGH_ALLOWED minus the two DL-50 honours (job_load, priority),
#: each with the effect it does NOT have.
_PASSTHROUGH_ATTRS: dict[str, str] = {
    "application": "the application tag is carried; nothing schedules or reports by it",
    "auto_delete": "the definition is never deleted; the catalog is static for the run",
    "avg_runtime": "the statistics seed is carried; no runtime estimate is computed from it",
    "chk_files": "the pre-start disk-space gate is not evaluated; the job starts regardless",
    "elevated": "no privilege elevation happens; the child runs as the invoking user",
    "group": "the group tag is carried; nothing schedules or reports by group",
    "interactive": "no interactive terminal is attached to the child process",
    "job_class": "job classes are not implemented; no class quota gates a start",
    "machine_method": "per-member placement method is not implemented; DL-49 resolves placement",
    "permission": "job permissions are not enforced; the run uses the invoking user's own",
    "ulimit": "no resource limit is applied to the child process",
}

_TIME_CLUSTER_ATTRS: dict[str, tuple[str, str, dict[str, str]]] = {
    # member: (cite, effect, the extra attributes its trigger needs)
    "days_of_week": (
        "SEM-30, SEM-31",
        "the days a schedule tick may fall on, as two-letter tokens or `all`",
        {"days_of_week": "all"},
    ),
    "run_calendar": (
        "SEM-30, DL-56",
        "the named calendar whose days are the schedule's day set",
        {"run_calendar": f'"{HOLCAL_NAME}"'},
    ),
    "exclude_calendar": (
        "SEM-30, DL-56",
        "the named calendar whose days are subtracted from the schedule's day set",
        {"exclude_calendar": f'"{HOLCAL_NAME}"'},
    ),
    "start_times": (
        "SEM-32",
        "the wall-clock times a schedule tick fires at on an eligible day",
        {"start_times": '"08:00"'},
    ),
    "start_mins": (
        "SEM-32",
        "the minutes past each hour a schedule tick fires at on an eligible day",
        {"start_mins": "0,30"},
    ),
    "run_window": (
        "SEM-33",
        "a gate, not a trigger: a start outside the window defers or drops, never fires early",
        {"run_window": '"09:00-10:00"'},
    ),
    "timezone": (
        "SEM-35",
        "the zone every schedule time on this job is read in",
        {"timezone": "UTC"},
    ),
    "must_start_times": (
        "SEM-34",
        "the RELATIVE form arms an alarm: a missed start raises MUST_START_ALARM and"
        " changes no status",
        {"start_times": '"08:00"', "must_start_times": '"+30"'},
    ),
    "must_complete_times": (
        "SEM-34",
        "the RELATIVE form arms an alarm: a missed completion raises MUST_COMPLETE_ALARM"
        " and changes no status",
        {"start_times": '"08:00"', "must_complete_times": '"+45"'},
    ),
}

_EXEC_ATTRS: dict[str, tuple[str, str, str, dict[str, str | None]]] = {
    # member: (klass, cite, effect, the trigger's attribute overrides)
    "machine": (
        SUPPORTED,
        "DL-49, DL-52",
        "names the machine the job runs on; the resolver refuses a foreign one"
        "; inert on a BOX (SEM-10)",
        {},
    ),
    "owner": (
        REFUSED,
        "runner_preflight._owner_preflight",
        "an owner other than the invoking user is refused at preflight: there is no setuid"
        "; inert on a BOX (SEM-10)",
        {"owner": "someone_else"},
    ),
    "profile": (
        SUPPORTED,
        "runner_adapters._build_run_spec",
        "sourced before the command runs (`. <profile> && <command>`); inert on a BOX (SEM-10)",
        {"profile": "/tmp/profile.sh"},
    ),
    "std_out_file": (
        SUPPORTED,
        "runner_adapters.job_log_paths",
        "the child's stdout appends here instead of the default run log; inert on a BOX (SEM-10)",
        {"std_out_file": "/tmp/out.log"},
    ),
    "std_err_file": (
        SUPPORTED,
        "runner_adapters.job_log_paths",
        "the child's stderr appends here instead of the default run log; inert on a BOX (SEM-10)",
        {"std_err_file": "/tmp/err.log"},
    ),
    "std_in_file": (
        SUPPORTED,
        "runner_adapters._build_run_spec",
        "the child reads stdin from here instead of /dev/null; inert on a BOX (SEM-10)",
        {"std_in_file": "/tmp/in.txt"},
    ),
    "envvars": (
        PASSTHROUGH,
        "ir._Lowerer._exec_spec, DL-32",
        "carried verbatim on the exec spec; the child process environment is not modified"
        "; inert on a BOX (SEM-10)",
        {"envvars": "A=1"},
    ),
}

_SEMANTICS_ATTRS: dict[str, tuple[str, str, str, dict[str, str | None]]] = {
    "condition": (
        SUPPORTED,
        "SEM-02, SEM-08",
        "the start gate: the job starts on the edge where its condition becomes true",
        {"condition": "s(J1)"},
    ),
    "box_success": (
        SUPPORTED,
        "SEM-12",
        "overrides a box's success verdict, evaluated on every member transition while"
        " the box is RUNNING, so an internal reference can finish the box early",
        {"job_type": "b", "command": None, "machine": None, "box_success": "s(J1)"},
    ),
    "box_failure": (
        SUPPORTED,
        "SEM-12",
        "overrides a box's failure verdict, evaluated on every member transition while"
        " the box is RUNNING; DECLARING it suppresses the matching default fold, so an"
        " override that never becomes true leaves the box RUNNING (SEM-12)",
        {"job_type": "b", "command": None, "machine": None, "box_failure": "f(J1)"},
    ),
    "max_exit_success": (
        SUPPORTED,
        "SEM-09, DL-33",
        "shifts the SUCCESS/FAILURE boundary: exit codes up to it are a success",
        {"max_exit_success": "2"},
    ),
    "success_codes": (
        SUPPORTED,
        "SEM-09, DL-33",
        "the explicit success set; with no fail_codes beside it, it alone decides the verdict",
        {"success_codes": "0,3-5"},
    ),
    "fail_codes": (
        SUPPORTED,
        "SEM-09, DL-33",
        "the explicit failure set; present, it is the only verdict source (Q7, DL-58)",
        {"fail_codes": "1,9"},
    ),
    "term_run_time": (
        SUPPORTED,
        "dossier ss5, oracle.Oracle._arm_sla_and_term",
        "arms a timer that TERMINATEs the run after n minutes",
        {"term_run_time": "10"},
    ),
    "n_retrys": (
        PASSTHROUGH,
        "DL-53",
        "the job runs without retries; preflight WARNs that the attribute is unmodelled",
        {"n_retrys": "2"},
    ),
    "auto_hold": (
        SUPPORTED,
        "dossier ss5",
        "the member enters ON_HOLD when its box starts, instead of starting with it",
        {"auto_hold": "1"},
    ),
    "status": (
        SUPPORTED,
        "SEM-24",
        "the definition-time status: only the out-of-band states are modelled, run states refuse",
        {"status": "ON_HOLD"},
    ),
}

_FW_JOB = {"job_type": "f", "command": None, "watch_file": "/tmp/watched"}

JOB_ATTR_ROWS: tuple[Row, ...] = (
    _rows(
        "job_attr",
        _ANNOTATION_ATTRS,
        klass=PASSTHROUGH,
        cite="dossier ss5, DL-32",
        effect="observability only: no alarm, notification or heartbeat is raised",
        trigger=lambda m: _job(**{m: "x"}),
    )
    + tuple(
        _row(
            surface="job_attr",
            member=member,
            klass=PASSTHROUGH,
            cite="dossier ss5, DL-32",
            effect=effect,
            trigger=_job(**{member: "1"}),
        )
        for member, effect in _PASSTHROUGH_ATTRS.items()
    )
    + (
        _row(
            surface="job_attr",
            member="job_load",
            klass=SUPPORTED,
            cite="DL-50",
            effect="the machine-load units a start holds against the machine's max_load",
            trigger=_job(job_load="1"),
        ),
        _row(
            surface="job_attr",
            member="job_load",
            facet="absent",
            klass=PROVISIONAL,
            cite="DL-50, ir.JobIR.job_load_units",
            label="Qr4",
            sites=("ir.JobIR.job_load_units#1",),
            effect="a job with no job_load demands zero machine-load units, so an unsized"
            " job never queues behind max_load",
            trigger=_job(),
            quiet=_job(job_load="1"),
        ),
        _row(
            surface="job_attr",
            member="priority",
            klass=SUPPORTED,
            cite="DL-50",
            effect="orders the QUE_WAIT queue; an undeclared priority sorts behind every"
            " declared one",
            trigger=_job(priority="10"),
        ),
        _row(
            surface="job_attr",
            member="priority",
            facet="direction",
            klass=PROVISIONAL,
            cite="DL-50, capacity.CapacityPool.sorted_waiters",
            label="Qr2",
            sites=(
                "capacity.CapacityPool.sorted_waiters.key#1",
                "ir.JobIR.priority_value#1",
            ),
            effect="a lower priority number is assumed to mean higher priority",
            trigger=_job(priority="1"),
            quiet=_job(priority="99"),
        ),
    )
    + tuple(
        _row(
            surface="job_attr",
            member=member,
            klass=SUPPORTED,
            cite=cite,
            effect=effect,
            trigger=_job(HOLCAL_BLOCK, date_conditions="1", **extra),
        )
        for member, (cite, effect, extra) in _TIME_CLUSTER_ATTRS.items()
    )
    + (
        _row(
            surface="job_attr",
            member="days_of_week",
            facet="absent",
            klass=PROVISIONAL,
            cite="SEM-30, runner_scheduler",
            label="E10",
            sites=("runner_scheduler.<module>#1",),
            effect="a schedule with no days_of_week is read as every day",
            trigger=_job(date_conditions="1", start_times='"08:00"'),
            quiet=_job(date_conditions="1", days_of_week="all", start_times='"08:00"'),
        ),
        _row(
            surface="job_attr",
            member="timezone",
            facet="dst-fold",
            klass=PROVISIONAL,
            cite="SEM-35, runner_scheduler",
            label="E10",
            sites=("runner_scheduler.Scheduler#1",),
            effect="a start time inside a DST fold or gap resolves by the pinned"
            " interpretation, not by a vendor-verified rule",
            trigger=_job(date_conditions="1", timezone="Europe/Berlin", start_times='"02:30"'),
            quiet=_job(date_conditions="1", timezone="UTC", start_times='"02:30"'),
        ),
        _row(
            surface="job_attr",
            member="must_start_times",
            facet="absolute",
            klass=PASSTHROUGH,
            cite="SEM-34, oracle.Oracle._arm_sla_and_term",
            effect="an ABSOLUTE must_start_times lowers and is carried, and arms nothing:"
            " the oracle owns no calendar, so no absolute deadline exists v1",
            trigger=_job(date_conditions="1", start_times='"08:00"', must_start_times='"08:30"'),
            quiet=_job(date_conditions="1", start_times='"08:00"', must_start_times='"+30"'),
        ),
        _row(
            surface="job_attr",
            member="must_complete_times",
            facet="absolute",
            klass=PASSTHROUGH,
            cite="SEM-34, oracle.Oracle._arm_sla_and_term",
            effect="an ABSOLUTE must_complete_times lowers and is carried, and arms"
            " nothing: the oracle owns no calendar, so no absolute deadline exists v1",
            trigger=_job(date_conditions="1", start_times='"08:00"', must_complete_times='"09:30"'),
            quiet=_job(date_conditions="1", start_times='"08:00"', must_complete_times='"+45"'),
        ),
        _row(
            surface="job_attr",
            member="exclude_calendar",
            facet="two-year-probe",
            klass=SUPPORTED,
            cite="DL-56, DL-57, runner_preflight._calendar_preflight",
            bound="732 dates inclusive, anchor through anchor+731 days",
            effect="preflight WARNs when the exclusion covers every eligible day it probes"
            " -- 732 dates inclusive, anchor through anchor+731 days; absence is proven"
            " within that bound only, and the run is warned, not refused",
            trigger=_job(
                HOLCAL_BLOCK,
                date_conditions="1",
                days_of_week="all",
                exclude_calendar=f'"{HOLCAL_NAME}"',
            ),
            quiet=_job(date_conditions="1", days_of_week="all"),
        ),
        _row(
            surface="job_attr",
            member="must_start_times",
            facet="unmatched-slot",
            klass=PROVISIONAL,
            cite="SEM-34, ir._Lowerer._sla_attr, oracle.Oracle._sla_offset",
            effect="an instant matching no start time uses the first offset; no label was"
            " opened for the corner",
            trigger=_job(
                date_conditions="1", start_times='"08:00,12:00"', must_start_times='"+30,+60"'
            ),
            quiet=_job(date_conditions="1", start_times='"08:00"', must_start_times='"+30"'),
        ),
        _row(
            surface="job_attr",
            member="must_complete_times",
            facet="unmatched-slot",
            klass=PROVISIONAL,
            cite="SEM-34, ir._Lowerer._sla_attr, oracle.Oracle._sla_offset",
            effect="an instant matching no start time uses the first offset; no label was"
            " opened for the corner",
            trigger=_job(
                date_conditions="1", start_times='"08:00,12:00"', must_complete_times='"+30,+60"'
            ),
            quiet=_job(date_conditions="1", start_times='"08:00"', must_complete_times='"+45"'),
        ),
    )
    + tuple(
        _row(
            surface="job_attr",
            member=member,
            klass=klass,
            cite=cite,
            effect=effect,
            trigger=_job(**extra) if extra else BASE_JIL,
            quiet=_job(machine=None) if member == "machine" else None,
        )
        for member, (klass, cite, effect, extra) in _EXEC_ATTRS.items()
    )
    + (
        _row(
            surface="job_attr",
            member="profile",
            facet="sourcing-failure",
            klass=PROVISIONAL,
            cite="runner_adapters._build_run_spec",
            label="E5",
            sites=(
                "runner_adapters.LocalCommandAdapter#1",
                "runner_adapters._build_run_spec#1",
            ),
            effect="a profile that fails to source fails the job with sh's exit code",
            trigger=_job(profile="/tmp/missing.sh"),
            quiet=_job(),
        ),
    )
    + tuple(
        _row(
            surface="job_attr",
            member=member,
            klass=klass,
            cite=cite,
            effect=effect,
            trigger=_job(BOX_BLOCK, box_name="BOX0", **extra)
            if member in ("box_success", "box_failure")
            else _job("insert_job: J1\njob_type: c\ncommand: true\nmachine: M0", **extra),
        )
        for member, (klass, cite, effect, extra) in _SEMANTICS_ATTRS.items()
    )
    + (
        _row(
            surface="job_attr",
            member="run_window",
            facet="equal-endpoints",
            klass=PROVISIONAL,
            cite="SEM-33, oracle.Oracle._run_window_permits",
            effect="a window whose endpoints are equal ADMITS that one instant --"
            " `lo <= now <= hi` is inclusive at both ends, so the window is not empty"
            " and not all day; the pin is undocumented and no label was opened for it",
            trigger=_job(date_conditions="1", days_of_week="all", run_window='"09:00-09:00"'),
            quiet=_job(date_conditions="1", days_of_week="all", run_window='"09:00-10:00"'),
        ),
        _row(
            surface="job_attr",
            member="run_window",
            facet="midpoint-tie",
            klass=PROVISIONAL,
            cite="SEM-33, oracle.Oracle._run_window_permits",
            effect="a start exactly halfway between the previous close and the next"
            " opening DEFERS to the opening rather than dropping; the tie is"
            " undocumented and no label was opened for it",
            trigger=_job(date_conditions="1", days_of_week="all", run_window='"09:00-10:00"'),
            quiet=_job(date_conditions="1", days_of_week="all", run_window='"06:00-07:00"'),
        ),
        _row(
            surface="job_attr",
            member="watch_file",
            facet="stat-error",
            klass=PROVISIONAL,
            cite="runner-design ss6, runner_adapters.FileWatcherAdapter",
            effect="EVERY stat error reads as the file being absent -- a permission"
            " denial is not told apart from a missing file -- and the watch resets its"
            " stable count and keeps polling; no label was opened for it",
            trigger=_job(job_type="f", command=None, watch_file="/nonexistent/dir/watched"),
            quiet=_job(**_FW_JOB),
        ),
        _row(
            surface="job_attr",
            member="resources",
            facet="duplicate",
            klass=REFUSED,
            cite="runner_preflight._resource_preflight, DL-50",
            effect="a job naming one resource twice is refused at preflight as ambiguous"
            " demand; a direct oracle caller instead SUMS the quantities and takes the"
            " most restrictive release policy",
            trigger=_job(RESOURCE_BLOCK, resources="(R0, QUANTITY=1) AND (R0, QUANTITY=2)"),
            quiet=_job(RESOURCE_BLOCK, resources="(R0, QUANTITY=1)"),
        ),
        _row(
            surface="job_attr",
            member="condition",
            facet="queued-no-recheck",
            klass=PROVISIONAL,
            cite="DL-50, oracle.Oracle._readmit",
            label="Qr6",
            sites=("oracle.<module>#1", "oracle.Oracle._readmit#1"),
            effect="a job admitted out of QUE_WAIT does not re-evaluate its condition",
            trigger=_job(
                "insert_job: J1\njob_type: c\ncommand: true\nmachine: M0",
                condition="s(J1)",
                job_load="1",
            ),
            quiet=_job(
                "insert_job: J1\njob_type: c\ncommand: true\nmachine: M0", condition="s(J1)"
            ),
        ),
        _row(
            surface="job_attr",
            member="box_success",
            facet="iced-member",
            klass=PROVISIONAL,
            cite="SEM-12, SEM-20",
            label="Q6",
            protocol="Q6",
            effect="an iced member is read as satisfied inside box_success, the same way"
            " it is read inside an ordinary condition",
            trigger=_job(
                BOX_BLOCK,
                box_name="BOX0",
                job_type="b",
                command=None,
                machine=None,
                box_success="s(J1)",
            ),
            quiet=_job(BOX_BLOCK, box_name="BOX0", job_type="b", command=None, machine=None),
        ),
    )
    + (
        _row(
            surface="job_attr",
            member="box_name",
            klass=SUPPORTED,
            cite="SEM-11",
            effect="names the box this job is a member of; the box's start starts the member",
            trigger=_job(BOX_BLOCK, box_name="BOX0"),
        ),
        _row(
            surface="job_attr",
            member="box_terminator",
            klass=SUPPORTED,
            cite="SEM-14",
            effect="this member's failure terminates the whole box",
            trigger=_job(BOX_BLOCK, box_name="BOX0", box_terminator="1"),
        ),
        _row(
            surface="job_attr",
            member="job_terminator",
            klass=SUPPORTED,
            cite="SEM-14",
            effect="this member is terminated when its box fails",
            trigger=_job(BOX_BLOCK, box_name="BOX0", job_terminator="1"),
        ),
        _row(
            surface="job_attr",
            member="job_type",
            klass=SUPPORTED,
            cite="SEM-10",
            effect="selects the modelled job kind: CMD, BOX or FW",
            trigger=BASE_JIL,
            quiet=GLOBAL_JIL,
        ),
        _row(
            surface="job_attr",
            member="command",
            klass=SUPPORTED,
            cite="runner-design ss6",
            effect="the shell command the CMD adapter spawns, passed to /bin/sh verbatim",
            trigger=BASE_JIL,
            quiet=_job(**_FW_JOB),
        ),
        _row(
            surface="job_attr",
            member="watch_file",
            klass=SUPPORTED,
            cite="runner-design ss6",
            effect="the path an FW job polls; the job completes only once the file exists,"
            " reaches watch_file_min_size, and two consecutive polls agree on its size",
            trigger=_job(**_FW_JOB),
        ),
        _row(
            surface="job_attr",
            member="watch_interval",
            klass=SUPPORTED,
            cite="runner-design ss6",
            effect="the poll interval of an FW job, in seconds",
            trigger=_job(watch_interval="30", **_FW_JOB),
        ),
        _row(
            surface="job_attr",
            member="watch_interval",
            facet="default",
            klass=PROVISIONAL,
            cite="runner_adapters.FileWatcherAdapter",
            label="E6",
            sites=("runner_adapters.FileWatcherAdapter.__init__#1",),
            effect="an FW job with no watch_interval polls at the profile's default interval",
            trigger=_job(**_FW_JOB),
            quiet=_job(watch_interval="30", **_FW_JOB),
        ),
        _row(
            surface="job_attr",
            member="watch_file_min_size",
            klass=SUPPORTED,
            cite="runner-design ss6",
            effect="the size the watched file must reach before the FW job completes",
            trigger=_job(watch_file_min_size="1024", **_FW_JOB),
        ),
        _row(
            surface="job_attr",
            member="watch_file_min_size",
            facet="steady-size",
            klass=PROVISIONAL,
            cite="runner_adapters.FileWatcherAdapter",
            label="E6",
            sites=("runner_adapters.FileWatcherAdapter#1",),
            effect="two consecutive qualifying polls must report the SAME size before the"
            " watch completes; a file still growing resets the count",
            trigger=_job(watch_file_min_size="1024", **_FW_JOB),
            quiet=_job(**_FW_JOB),
        ),
        _row(
            surface="job_attr",
            member="date_conditions",
            klass=SUPPORTED,
            cite="SEM-30",
            effect="the master switch: the time cluster is honoured only when it is truthy",
            trigger=_job(date_conditions="1", days_of_week="all"),
        ),
        _row(
            surface="job_attr",
            member="resources",
            klass=SUPPORTED,
            cite="DL-21, DL-50",
            effect="the resource groups a start must satisfy before it may run",
            trigger=_job(RESOURCE_BLOCK, resources="(R0, QUANTITY=1)"),
        ),
        _row(
            surface="job_attr",
            member="QUANTITY",
            klass=SUPPORTED,
            cite="DL-21, DL-50",
            effect="the units one resources group demands; a group without it is refused",
            trigger=_job(RESOURCE_BLOCK, resources="(R0, QUANTITY=2)"),
        ),
        _row(
            surface="job_attr",
            member="FREE",
            klass=SUPPORTED,
            cite="DL-50",
            effect="overrides the resource's release default for this one request",
            trigger=_job(RESOURCE_BLOCK, resources="(R0, QUANTITY=1, FREE=Y)"),
        ),
    )
)


# --------------------------------------------- machine / resource / xinst / global

_ANY_ATTR_EFFECT = "any other {kind} attribute is carried verbatim; its effect is not implemented"

MACHINE_ATTR_ROWS: tuple[Row, ...] = (
    _row(
        surface="machine_attr",
        member="type",
        klass=SUPPORTED,
        cite="DL-49",
        effect="selects the resolver's machine kind: a, r, n or v",
        trigger=BASE_JIL,
        quiet=GLOBAL_JIL,
    ),
    _row(
        surface="machine_attr",
        member="node_name",
        klass=SUPPORTED,
        cite="DL-49, DL-52",
        effect="the host an agent or real machine resolves to",
        trigger=BASE_JIL,
        quiet=GLOBAL_JIL,
    ),
    _row(
        surface="machine_attr",
        member="machine",
        klass=SUPPORTED,
        cite="DL-49",
        effect="opens one pool member inside a virtual machine",
        trigger=_estate(
            "insert_machine: M0\ntype: v\nmachine: A1\nmachine: A2",
            "insert_machine: A1\ntype: a\nnode_name: localhost",
            "insert_machine: A2\ntype: a\nnode_name: localhost",
            JOB_BLOCK,
        ),
    ),
    _row(
        surface="machine_attr",
        member="max_load",
        klass=SUPPORTED,
        cite="DL-50",
        effect="sizes the machine's load bucket; an absent one is AutoSys's unlimited default",
        trigger=_estate(
            "insert_machine: M0\ntype: a\nnode_name: localhost\nmax_load: 10", JOB_BLOCK
        ),
    ),
    _row(
        surface="machine_attr",
        member="max_load",
        facet="pool",
        klass=PROVISIONAL,
        cite="DL-49, runner_preflight._resource_preflight",
        label="Qr3",
        sites=("runner_preflight._resource_preflight#1",),
        effect="a pool machine carries no load throttle; preflight WARNs and the job runs",
        trigger=_estate(
            "insert_machine: M0\ntype: v\nmachine: A1\nmax_load: 5",
            "insert_machine: A1\ntype: a\nnode_name: localhost",
            LOADED_JOB_BLOCK,
        ),
        quiet=_estate(
            "insert_machine: M0\ntype: a\nnode_name: localhost\nmax_load: 5", LOADED_JOB_BLOCK
        ),
    ),
    _row(
        surface="machine_attr",
        member="factor",
        klass=PASSTHROUGH,
        cite="DL-49",
        effect="the per-member load factor is carried; per-member placement is unmodelled",
        trigger=_estate(
            "insert_machine: M0\ntype: v\nmachine: A1\nfactor: 1.0",
            "insert_machine: A1\ntype: a\nnode_name: localhost",
            JOB_BLOCK,
        ),
    ),
    _row(
        surface="machine_attr",
        member="*",
        klass=PASSTHROUGH,
        cite="ir._Lowerer._lower_machine, DL-28",
        effect=_ANY_ATTR_EFFECT.format(kind="machine"),
        trigger=_estate(
            "insert_machine: M0\ntype: a\nnode_name: localhost\nopsys: unix", JOB_BLOCK
        ),
    ),
)

RESOURCE_ATTR_ROWS: tuple[Row, ...] = (
    _row(
        surface="resource_attr",
        member="res_type",
        klass=SUPPORTED,
        cite="DL-50",
        effect="sets the resource's default release: R renewable, D depletable, T threshold",
        trigger=_stmt(RESOURCE_BLOCK),
    ),
    _row(
        surface="resource_attr",
        member="res_type",
        facet="absent",
        klass=SUPPORTED,
        cite="DL-50, capacity.resource_type",
        effect="a resource with no res_type reads as renewable",
        trigger=_stmt("insert_resource: R0\namount: 4"),
        quiet=_stmt(RESOURCE_BLOCK),
    ),
    _row(
        surface="resource_attr",
        member="res_type",
        facet="unknown",
        klass=REFUSED,
        cite="runner_preflight._resource_preflight, DL-50",
        effect="a res_type outside R/D/T has unknown release semantics and refuses the run",
        trigger=_job("insert_resource: R0\nres_type: Z\namount: 4", resources="(R0, QUANTITY=1)"),
        quiet=_job(RESOURCE_BLOCK, resources="(R0, QUANTITY=1)"),
    ),
    _row(
        surface="resource_attr",
        member="amount",
        klass=SUPPORTED,
        cite="DL-50",
        effect="sizes the resource's semaphore bucket",
        trigger=_stmt(RESOURCE_BLOCK),
    ),
    _row(
        surface="resource_attr",
        member="amount",
        facet="absent",
        klass=REFUSED,
        cite="runner_preflight._resource_preflight, DL-50",
        effect="an unsized resource a job requires refuses the run; the semaphore cannot be sized",
        trigger=_job("insert_resource: R0\nres_type: R", resources="(R0, QUANTITY=1)"),
        quiet=_job(RESOURCE_BLOCK, resources="(R0, QUANTITY=1)"),
    ),
    _row(
        surface="resource_attr",
        member="*",
        klass=PASSTHROUGH,
        cite="ir._Lowerer._lower_resource, DL-28",
        effect=_ANY_ATTR_EFFECT.format(kind="resource"),
        trigger=_stmt(RESOURCE_BLOCK + "\ndescription: spare"),
    ),
)

XINST_ATTR_ROWS: tuple[Row, ...] = (
    _row(
        surface="xinst_attr",
        member="xtype",
        klass=SUPPORTED,
        cite="SEM-07, DL-28",
        effect="the external instance's type; a definition without it is refused",
        trigger=_stmt("insert_xinst: X0\nxtype: a"),
    ),
    _row(
        surface="xinst_attr",
        member="*",
        klass=PASSTHROUGH,
        cite="ir._Lowerer._lower_xinst, DL-28",
        effect=_ANY_ATTR_EFFECT.format(kind="external-instance"),
        trigger=_stmt("insert_xinst: X0\nxtype: a\nxmachine: other_host"),
    ),
)

GLOBAL_ATTR_ROWS: tuple[Row, ...] = (
    _row(
        surface="global_attr",
        member="value",
        klass=SUPPORTED,
        cite="SEM-08",
        effect="the global's declared value, unquoted, as value() comparands read it",
        trigger=_stmt("insert_global: G0\nvalue: 0"),
    ),
)


# ------------------------------------------------------------- calendar_attr

_CALENDAR_ATTRS: dict[str, tuple[str, str, str]] = {
    # member: (cite, effect, the calendar body line its trigger carries)
    "description": (
        "SEM-36",
        "carried on the calendar record; no rule reads it",
        "description: quarter ends",
    ),
    "workday": (
        "SEM-36",
        "the weekday mask every workday-scoped token and W/P walk counts in",
        "workday: xxxxx..",
    ),
    "non_workday": (
        "SEM-36, SEM-38",
        "what happens to a generated day that is not a workday: filter or replacement",
        "non_workday: O",
    ),
    "holiday": (
        "SEM-36, SEM-38",
        "what happens to a generated day that is a holiday; it governs holcal dates outright",
        "holiday: S",
    ),
    "holcal": (
        "SEM-36",
        "names the standard calendar whose days are this calendar's holidays",
        "",
    ),
    "cyccal": (
        "SEM-36, SEM-39",
        "names the cycle whose periods the cycle-scoped tokens count in",
        "",
    ),
    "adjust": (
        "SEM-36, SEM-38",
        "a uniform blind day shift applied to every surviving day; the documented"
        " range is -9..+9 and anything outside it refuses the calendar",
        "adjust: 1",
    ),
}

#: Calendar attributes the engine carries and never reads.
_CALENDAR_PASSTHROUGH = frozenset({"description"})

CALENDAR_ATTR_ROWS: tuple[Row, ...] = tuple(
    _row(
        surface="calendar_attr",
        member=member,
        klass=PASSTHROUGH if member in _CALENDAR_PASSTHROUGH else SUPPORTED,
        cite=cite,
        effect=effect,
        trigger=_cal(
            "condition: DAILY",
            *([line] if line else []),
            cyccal=member == "cyccal",
            holcal=member == "holcal",
        ),
    )
    for member, (cite, effect, line) in _CALENDAR_ATTRS.items()
) + (
    _row(
        surface="calendar_attr",
        member="condition",
        klass=SUPPORTED,
        cite="SEM-37, DL-57",
        effect="one date-condition rule; the rules of a calendar union into its day set",
        trigger=_cal("condition: DAILY"),
    ),
    _row(
        surface="calendar_attr",
        member="start_date",
        klass=SUPPORTED,
        cite="SEM-39",
        effect="opens one cycle period; it pairs positionally with the end_date after it",
        trigger=_stmt("cycle: CY0\nstart_date: 01/01/2026\nend_date: 03/31/2026"),
    ),
    _row(
        surface="calendar_attr",
        member="end_date",
        klass=SUPPORTED,
        cite="SEM-39",
        effect="closes the cycle period its preceding start_date opened",
        trigger=_stmt("cycle: CY0\nstart_date: 01/01/2026\nend_date: 03/31/2026"),
    ),
    _row(
        surface="calendar_attr",
        member="adjust",
        facet="with-replacement",
        klass=PROVISIONAL,
        cite="SEM-38, DL-59",
        label="Q8b",
        sites=("autocal.compile_calendar#1",),
        protocol="Q8b",
        effect="disposition replaces first, then the blind adjust shifts every survivor",
        trigger=_cal("condition: DAILY", "adjust: 1", "non_workday: N"),
        quiet=_cal("condition: DAILY", "adjust: 1"),
    ),
    _row(
        surface="calendar_attr",
        member="workday",
        facet="absent",
        klass=SUPPORTED,
        cite="SEM-36, autocal.compile_calendar",
        effect="an absent or blank workday is Monday to Friday",
        trigger=_cal("condition: DAILY"),
        quiet=_cal("condition: DAILY", "workday: all"),
    ),
    _row(
        surface="calendar_attr",
        member="non_workday",
        facet="absent",
        klass=SUPPORTED,
        cite="SEM-38, autocal.CompiledCalendar._dispose",
        effect="with no non_workday action a generated day is kept exactly as it falls",
        trigger=_cal("condition: DAILY"),
        quiet=_cal("condition: DAILY", "non_workday: O"),
    ),
    _row(
        surface="calendar_attr",
        member="holiday",
        facet="absent",
        klass=SUPPORTED,
        cite="SEM-38, DL-58, autocal.CompiledCalendar._dispose",
        effect="with no holiday action a holcal date gets no treatment of its own: it"
        " falls through to the non_workday branch, which only acts on a day that is"
        " not a workday, so a holiday ON a workday is kept untouched",
        trigger=_cal("condition: DAILY", holcal=True),
        quiet=_cal("condition: DAILY", "holiday: S", holcal=True),
    ),
    _row(
        surface="calendar_attr",
        member="adjust",
        facet="absent",
        klass=SUPPORTED,
        cite="SEM-36, autocal.compile_calendar",
        effect="an absent or blank adjust is zero: no day is shifted",
        trigger=_cal("condition: DAILY"),
        quiet=_cal("condition: DAILY", "adjust: 1"),
    ),
    _row(
        surface="calendar_attr",
        member="workday",
        facet="all",
        klass=SUPPORTED,
        cite="SEM-36, DL-60",
        effect="the observed `all` serialization makes every day a workday",
        trigger=_cal("condition: DAILY", "workday: all"),
        quiet=_cal("condition: DAILY"),
    ),
    _row(
        surface="calendar_attr",
        member="workday",
        facet="mask",
        klass=SUPPORTED,
        cite="SEM-36",
        effect="the positional seven-character mask reads Monday first",
        trigger=_cal("condition: DAILY", "workday: xxxxx.."),
        quiet=_cal("condition: DAILY"),
    ),
    _row(
        surface="calendar_attr",
        member="workday",
        facet="codes",
        klass=SUPPORTED,
        cite="SEM-36",
        effect="the comma list of day codes is the third accepted serialization",
        trigger=_cal("condition: DAILY", "workday: mo,tu,we,th,fr"),
        quiet=_cal("condition: DAILY"),
    ),
)


# --------------------------------------------------------- value-alternative surfaces

VALUE_ROWS: tuple[Row, ...] = (
    _rows(
        "job_type",
        ("b", "box", "c", "cmd", "f", "fw"),
        klass=SUPPORTED,
        cite="SEM-10",
        effect="the {member} spelling selects its modelled job kind",
        trigger=lambda m: (
            _job(job_type=m, command=None, machine=None)
            if m in ("b", "box")
            else _job(job_type=m, command=None, watch_file="/tmp/watched")
            if m in ("f", "fw")
            else _job(job_type=m)
        ),
        quiet=GLOBAL_JIL,
    )
    + _rows(
        "bool_spelling",
        ("0", "1", "false", "n", "no", "true", "y", "yes"),
        klass=SUPPORTED,
        cite="ir._Lowerer._bool_attr",
        effect="{member} is accepted as a boolean attribute value, case-insensitively",
        trigger=lambda m: _job(date_conditions=m, days_of_week="all"),
    )
    + _rows(
        "day_token",
        (
            "all",
            "fr",
            "friday",
            "mo",
            "monday",
            "sa",
            "saturday",
            "su",
            "sunday",
            "th",
            "thursday",
            "tu",
            "tuesday",
            "we",
            "wednesday",
        ),
        klass=SUPPORTED,
        cite="SEM-30, ir._Lowerer._days_of_week",
        effect="{member} is accepted in days_of_week and folds to its two-letter token",
        trigger=lambda m: _job(date_conditions="1", days_of_week=m),
    )
    + _rows(
        "initial_status",
        ("INACTIVE", "ON_HOLD", "ON_ICE", "ON_NOEXEC"),
        klass=SUPPORTED,
        cite="SEM-24",
        effect="{member} at definition time is modelled; run states are refused, never guessed",
        trigger=lambda m: _job(status=m),
    )
    + tuple(
        _row(
            surface="machine_type",
            member=member,
            klass=SUPPORTED,
            cite="DL-49",
            effect=f"machine type {member} resolves to a host the DL-52 identity rules compare",
            trigger=_machine_estate(member),
            # every estate states SOME type, so the quiet fixture states another
            quiet=_machine_estate("a" if member == "n" else "n"),
        )
        for member in ("a", "n", "r", "v")
    )
    + (
        _row(
            surface="machine_type",
            member="v",
            facet="load-throttle",
            klass=PROVISIONAL,
            cite="DL-49, ir.MachineIR.max_load_units",
            label="Qr3",
            sites=("ir.MachineIR.max_load_units#1",),
            effect="a virtual machine carries no machine-load throttle of its own",
            trigger=_estate(
                "insert_machine: M0\ntype: v\nmachine: A1\nmax_load: 5",
                "insert_machine: A1\ntype: a\nnode_name: localhost",
                LOADED_JOB_BLOCK,
            ),
            quiet=_estate(
                "insert_machine: M0\ntype: a\nnode_name: localhost\nmax_load: 5",
                LOADED_JOB_BLOCK,
            ),
        ),
    )
    + _rows(
        "res_type",
        ("D", "R", "T"),
        klass=SUPPORTED,
        cite="DL-50",
        effect="res_type {member} sets the bucket's default release behaviour",
        trigger=lambda m: _job(
            f"insert_resource: R0\nres_type: {m}\namount: 4", resources="(R0, QUANTITY=1)"
        ),
    )
    + _rows(
        "free_code",
        ("A", "N", "Y"),
        klass=SUPPORTED,
        cite="DL-50",
        effect="FREE={member} overrides the resource's release default for one request",
        trigger=lambda m: _job(RESOURCE_BLOCK, resources=f"(R0, QUANTITY=1, FREE={m})"),
    )
    + _rows(
        "release_policy",
        ("completion", "never", "success"),
        klass=SUPPORTED,
        cite="DL-50, capacity.release_policy",
        effect="units are released on {member}",
        trigger=lambda m: _job(
            RESOURCE_BLOCK,
            resources="(R0, QUANTITY=1, FREE=%s)"
            % {"completion": "A", "never": "N", "success": "Y"}[m],
        ),
    )
    + (
        _row(
            surface="release_policy",
            member="completion",
            facet="free-absent",
            klass=PROVISIONAL,
            cite="DL-50, capacity.release_policy",
            label="Qr1",
            effect="a request with no FREE takes the res_type default, renewable for an"
            " absent res_type",
            trigger=_job(RESOURCE_BLOCK, resources="(R0, QUANTITY=1)"),
            quiet=_job(RESOURCE_BLOCK, resources="(R0, QUANTITY=1, FREE=A)"),
        ),
    )
)


# ------------------------------------------------------------------ conditions

#: Four rule nodes appear in EVERY parse (`start`, `expr`, `atom_or_group`,
#: `atom`), so no non-empty condition can be their quiet fixture; they fall
#: back to the no-condition base, where nothing parses and nothing is seen.
#: Every other row names a condition that discriminates.
_COND_RULES: dict[str, tuple[str, str, str | None]] = {
    # member: (effect, the condition that produces the node, a quiet condition)
    "start": ("the whole condition expression is one parse", "s(J1)", None),
    "expr": ("an expression, flat left-to-right over & and |", "s(J1)", None),
    "atom_or_group": ("an atom or a parenthesised expression", "s(J1)", None),
    "atom": ("one of the three atom kinds", "s(J1)", None),
    "op": ("the operator between two operands", "s(J1) & f(J2)", "s(J1)"),
    "status_atom": (
        "a job-status test, optionally qualified by a lookback",
        "s(J1)",
        "v(G0)=1",
    ),
    "exitcode_atom": (
        "an exit-code comparison against a job's last run",
        "e(J1)=0",
        "s(J1)",
    ),
    "global_atom": ("a global-variable comparison", "v(G0)=1", "s(J1)"),
    "job_ref": (
        "a job name, with an optional cross-instance suffix",
        "s(J1)",
        "v(G0)=1",
    ),
    "lookback": ("the SEM-04 lookback qualifier on an atom", "s(J1,1.30)", "s(J1)"),
    "global_name": ("the global variable's name", "v(G0)=1", "s(J1)"),
    "global_value": ("the comparand, quoted or bare", "v(G0)=1", "s(J1)"),
    "binop": ("two operands joined by one operator", "s(J1) & f(J2)", "s(J1)"),
    "paren": ("an explicitly grouped subexpression", "(s(J1))", "s(J1)"),
}

_COND_TERMINALS: dict[str, tuple[str, str | None]] = {
    # member: (a condition whose lexing yields it, one that does not)
    "STATUS_KW=success": ("success(J1)", "s(J1)"),
    "STATUS_KW=failure": ("failure(J1)", "s(J1)"),
    "STATUS_KW=done": ("done(J1)", "s(J1)"),
    "STATUS_KW=terminated": ("terminated(J1)", "s(J1)"),
    "STATUS_KW=notrunning": ("notrunning(J1)", "s(J1)"),
    "STATUS_KW=s": ("s(J1)", "f(J1)"),
    "STATUS_KW=f": ("f(J1)", "s(J1)"),
    "STATUS_KW=d": ("d(J1)", "s(J1)"),
    "STATUS_KW=t": ("t(J1)", "s(J1)"),
    "STATUS_KW=n": ("n(J1)", "s(J1)"),
    "EXITCODE_KW=exitcode": ("exitcode(J1)=0", "e(J1)=0"),
    "EXITCODE_KW=e": ("e(J1)=0", "exitcode(J1)=0"),
    "VALUE_KW=value": ("value(G0)=1", "v(G0)=1"),
    "VALUE_KW=v": ("v(G0)=1", "value(G0)=1"),
    "JOB_NAME": ("s(J1)", "v(G0)=1"),
    "INSTANCE_NAME": ("s(J1^PROD)", "s(J1)"),
    "LOOKBACK_TOKEN": ("s(J1,1.30)", "s(J1)"),
    "CMP_OP==": ("v(G0)=1", "s(J1)"),
    "CMP_OP=!=": ("v(G0)!=1", "v(G0)=1"),
    "CMP_OP=<": ("v(G0)<1", "v(G0)=1"),
    "CMP_OP=>": ("v(G0)>1", "v(G0)=1"),
    "CMP_OP=<=": ("v(G0)<=1", "v(G0)=1"),
    "CMP_OP=>=": ("v(G0)>=1", "v(G0)=1"),
    "GLOBAL_NAME": ("v(G0)=1", "s(J1)"),
    "QUOTED": ('v(G0)="x"', "v(G0)=1"),
    "BARE_VALUE": ("v(G0)=1", 'v(G0)="x"'),
    "AND=&": ("s(J1)&f(J2)", "s(J1)|f(J2)"),
    "AND=and": ("s(J1) and f(J2)", "s(J1)&f(J2)"),
    "OR=|": ("s(J1)|f(J2)", "s(J1)&f(J2)"),
    "OR=or": ("s(J1) or f(J2)", "s(J1)|f(J2)"),
    # terminals the grammar TEXT does not define: two %import-ed from
    # lark's common set, four anonymous ones lark builds from the quoted
    # punctuation inside the rules (R-b, DL-209)
    "INT": ("e(J1)=0", "s(J1)"),
    "WS": ("s(J1) & f(J2)", "s(J1)&f(J2)"),
    # every atom form carries its own parentheses, so no parseable
    # condition can be their quiet: they fall back to the no-condition base
    "LPAR": ("s(J1)", None),
    "RPAR": ("s(J1)", None),
    "COMMA": ("s(J1,1.30)", "s(J1)"),
    "CIRCUMFLEX": ("s(J1^PROD)", "s(J1)"),
}

#: The regex-bodied terminals, with the pattern PINNED as the built parser
#: reports it and what it admits in words (DL-209). An edit to any of these
#: regexes fails until somebody re-reads what the new one accepts.
_TERMINAL_PATTERNS: dict[str, tuple[str, str]] = {
    "JOB_NAME": (
        r"(?:[^\s(),^&|:\\]|\\:)+",
        "a job name: any run of characters except whitespace, parentheses, comma,"
        " caret, the operators and a bare colon; a colon inside a name is escaped",
    ),
    "INSTANCE_NAME": (
        r"[A-Za-z0-9_#@$]+",
        "a cross-instance suffix: letters, digits, underscore, hash, at or dollar",
    ),
    "LOOKBACK_TOKEN": (
        r"\d{1,4}(\.\d{1,2}|\\:\d{1,2})?",
        "three lookback spellings: bare hours, `hhhh.mm` and `hhhh\\:mm`, with one to"
        " four hour digits and one or two minute digits; the mm RANGE is checked at"
        " lowering, not here",
    ),
    "GLOBAL_NAME": (
        r"[^\s(),=<>!&|]+",
        "a global variable name: anything but whitespace, parentheses, comma, the"
        " comparison characters and the operators",
    ),
    "QUOTED": (
        r'"[^"]*"',
        "a double-quoted comparand with no interior quote; the quotes are stripped"
        " from the semantic value",
    ),
    "BARE_VALUE": (
        r"[^\s()&|]+",
        "an unquoted comparand: anything but whitespace, parentheses and the operators",
    ),
    "INT": (
        r"(?:[0-9])+",
        "the integer an exitcode_atom compares against, ASCII digits only",
    ),
    "WS": (
        "(?:[ \t\x0c\r\n])+",
        "whitespace between tokens -- space, tab, form feed, carriage return"
        " or newline -- lexed and then discarded, so it is never a token",
    ),
}

#: What each of those six punctuates or carries, for the row's effect.
_IMPLICIT_TERMINAL_EFFECTS: dict[str, str] = {
    "INT": "the integer an exitcode_atom compares against (SEM-02)",
    "LPAR": "opens an atom's argument list and a parenthesised group",
    "RPAR": "closes an atom's argument list and a parenthesised group",
    "COMMA": "separates a job reference from its lookback qualifier (SEM-04)",
    "CIRCUMFLEX": "introduces the cross-instance suffix of a job reference (SEM-07)",
}

COND_ROWS: tuple[Row, ...] = (
    tuple(
        _row(
            surface="cond_rule",
            member=member,
            klass=SUPPORTED,
            cite="SEM-02, SEM-03",
            effect=effect,
            trigger=text,
            quiet=quiet,
        )
        for member, (effect, text, quiet) in _COND_RULES.items()
    )
    + tuple(
        _row(
            surface="cond_terminal",
            member=member,
            klass=SUPPORTED,
            cite="SEM-02, SEM-03, SEM-04",
            pattern=_TERMINAL_PATTERNS[member][0] if member in _TERMINAL_PATTERNS else None,
            effect=(
                _TERMINAL_PATTERNS[member][1]
                if member in _TERMINAL_PATTERNS
                else _IMPLICIT_TERMINAL_EFFECTS.get(
                    member,
                    f"the grammar lexes {member} and the transformer gives it its SEM meaning",
                )
            ),
            trigger=text,
            quiet=quiet,
        )
        for member, (text, quiet) in _COND_TERMINALS.items()
    )
    + _rows(
        "lookback_kind",
        ("indefinite", "window", "zero"),
        klass=SUPPORTED,
        cite="SEM-04",
        effect="the {member} lookback shape is modelled by the oracle's atom evaluation",
        trigger=lambda m: {
            "indefinite": "s(J1,9999)",
            "window": "s(J1,1.30)",
            "zero": "s(J1,0)",
        }[m],
        quiet="s(J1)",
    )
    + _rows(
        "atom_status",
        ("DONE", "FAILURE", "NOTRUNNING", "SUCCESS", "TERMINATED"),
        klass=SUPPORTED,
        cite="SEM-02, SEM-05",
        effect="a {member} atom reads the referenced job's current recorded status",
        trigger=lambda m: f"{m.lower()}(J1)",
        quiet="v(G0)=1",
    )
    + (
        _row(
            surface="cond_rule",
            member="job_ref",
            facet="cross-instance",
            klass=SUPPORTED,
            cite="SEM-07, oracle.Oracle._atom_true",
            effect="a job^INST atom reads an instance-qualified pseudo-job that only an"
            " injected STATUS can set",
            trigger="s(J1^PROD)",
            quiet="s(J1)",
        ),
    )
)


# ------------------------------------------------------------------ calendars

_CAL_KEYWORDS: tuple[str, ...] = (
    "daily",
    "workdays",
    "weekdays",
    "fomwork",
    "eomwork",
    "fomweek",
    "eomweek",
    "fom",
    "eom",
    "cycle",
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "sat",
    "sun",
)

#: family name -> (a token of that family, its exact pattern, what the
#: pattern admits in words). The pattern is PINNED: the test holds it equal
#: to `autocal._FAMILIES`, so editing the regex fails until somebody re-reads
#: what it now accepts (DL-209). `#` counts from the start, `M` from the end,
#: `X` excludes, and `L` is the last one.
_CAL_FAMILIES: dict[str, tuple[str, str, str]] = {
    # family name: (a token of that family, pattern, alternatives)
    "workd": (
        "WORKD#1",
        "workd([#m])(\\d+|l)",
        "the nth workday of the month, counted from the start (`#`) or the end (`M`), 1..31 or `L`",
    ),
    "weekd": (
        "WEEKD#1",
        "weekd([#mx])(\\d+|l)",
        "the nth day of the week, from the start (`#`), the end (`M`) or excluded (`X`), 1..7 or `L`",
    ),
    "wekr": (
        "WEKRMON#1",
        "wekr(mon|tue|wed|thu|fri|sat|sun)([#mx])(\\d+|l)",
        "the nth day of a week anchored on a named weekday, `#`/`M`/`X`, 1..7 or `L`",
    ),
    "week_parity": ("WEEK#E", "week#([eo])", "every even (`E`) or odd (`O`) week of the year"),
    "week": (
        "WEEK#1",
        "week([#mx])(\\d+|l)",
        "the nth week of the year, `#`/`M`/`X`, 1..53 or `L`",
    ),
    "mnthd": (
        "MNTHD#1",
        "mnthd([#mx])(\\d+|l)",
        "the nth day of the month, `#`/`M`/`X`, 1..31 or `L`",
    ),
    "month_ordinal": (
        "JAN#1",
        "(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)([#m])(\\d+|l)",
        "the nth day of a named month, `#`/`M`, 1..31 or `L`",
    ),
    "day_ordinal": (
        "MON#1",
        "(mon|tue|wed|thu|fri|sat|sun)([#m])(\\d|l)",
        "the nth named weekday of the month, `#`/`M`, a SINGLE digit 1..5 or `L`",
    ),
    "cycl": (
        "CYCL#1",
        "cycl([#mx])(\\d+|l)",
        "the nth day of a cycle period, `#`/`M`/`X`, 1..365 or `L`",
    ),
    "cycp": (
        "CYCP#1",
        "cycp#(\\d+)",
        "the nth cycle period itself, 1..30; no from-end or excluded form",
    ),
    "cweek_parity": (
        "CWEEK#E",
        "cweek#([eol])",
        "every even (`E`) or odd (`O`) chunk of a period, or its last (`L`)",
    ),
    "cweek": (
        "CWEEK#2",
        "cweek([#mx])(\\d+)",
        "the nth seven-day chunk of a cycle period, `#`/`M`/`X`, 1..53; `L` belongs to the parity form",
    ),
    "cwrk": (
        "CWRK#1",
        "cwrk([#mx])(\\d+|l)",
        "the nth workday of a cycle period, `#`/`M`/`X`, 1..365 or `L`",
    ),
    "cddd": (
        "CMON#1",
        "c(mon|tue|wed|thu|fri|sat|sun)([#m])(\\d+|l)",
        "the nth named weekday of a cycle period, `#`/`M`, 1..53 or `L`",
    ),
}

#: The families whose tokens only mean anything inside a cycle's periods;
#: their calendars need a `cyccal` or `compile_calendar` refuses them.
_CYCLE_SCOPED_FAMILIES = frozenset({"cycl", "cycp", "cweek_parity", "cweek", "cwrk", "cddd"})

_DEFECTIVE_FAMILIES: dict[str, tuple[str, str, str]] = {
    "workdx": (
        "WORKDX1",
        r"workdx\d+",
        "an excluded workday ordinal whose text contradicts its month-scoped siblings",
    ),
    "cwek": (
        "CWEK#1",
        r"cwek(#(\d|l)|m\d|x\d)",
        "a cycle-week ordinal, `#`/`M`/`X` with one digit or `L`, whose definitions are"
        " garbled in the vendor's own render",
    ),
}

_CAL_OPERATORS: dict[str, tuple[str, str]] = {
    # member: (effect, the condition line its trigger carries)
    "(": ("groups a subexpression", "condition: (DAILY | MON)"),
    ")": ("closes a grouped subexpression", "condition: (DAILY | MON)"),
    "{": ("the observed brace spelling of `(`", "condition: {DAILY} | {MON}"),
    "}": ("the observed brace spelling of `)`", "condition: {DAILY} | {MON}"),
    "&": ("intersects two operands", "condition: MON & JAN"),
    "|": ("unions two operands", "condition: MON | TUE"),
    "and": ("the word synonym of `&`", "condition: MON AND JAN"),
    "or": ("the word synonym of `|`", "condition: MON OR TUE"),
    "not": ("complements its operand", "condition: NOT MON"),
    "x": ("the X- prefix reads a token as its complement", "condition: DAILY & XMON"),
    ",": ("separates the rules of one calendar", "condition: MON,TUE"),
}

#: What each (category, action) pair DOES (SEM-38). The pair is the
#: behaviour, not the letter: N advances exactly one calendar day for a
#: holiday and walks to the next non-holiday workday for a non-workday, and
#: S is a no-op for a non-workday but SHIELDS a holiday from the non-workday
#: action (DL-58).
_ACTION_EFFECTS: dict[tuple[str, str], str] = {
    ("non_workday", "o"): "restrict to non-workdays: a generated day that IS a workday is dropped",
    ("non_workday", "s"): "keep the date unchanged; the day is generated as it falls",
    ("non_workday", "n"): "replace the date with the next workday that is also not a holiday",
    ("non_workday", "w"): "walk FORWARD to the next workday and use that date",
    ("non_workday", "p"): "walk BACKWARD to the previous workday and use that date",
    ("holiday", "o"): "restrict to holidays: a generated day that is not a holiday is dropped",
    ("holiday", "s"): "keep the holiday unchanged, and shield it from the non_workday action",
    ("holiday", "n"): "replace the holiday with the NEXT CALENDAR DAY, even if that day is"
    " itself a holiday or a non-workday",
    ("holiday", "w"): "walk FORWARD to the next non-holiday workday and use that date",
    ("holiday", "p"): "walk BACKWARD to the previous non-holiday workday and use that date",
}

CALENDAR_ROWS: tuple[Row, ...] = (
    _rows(
        "cal_keyword",
        _CAL_KEYWORDS,
        klass=SUPPORTED,
        cite="SEM-37",
        effect="the {member} keyword generates its documented day set",
        trigger=lambda m: _cal(f"condition: {m.upper()}", cyccal=(m == "cycle")),
    )
    + tuple(
        _row(
            surface="cal_family",
            member=member,
            klass=SUPPORTED,
            cite="SEM-37",
            pattern=pattern,
            effect=f"{words}",
            trigger=_cal(f"condition: {token}", cyccal=member in _CYCLE_SCOPED_FAMILIES),
        )
        for member, (token, pattern, words) in _CAL_FAMILIES.items()
    )
    + tuple(
        _row(
            surface="cal_family",
            member=member,
            klass=REFUSED,
            cite="autocal._parse_token, SEM-37",
            pattern=pattern,
            effect=f"{words}; refused rather than guessed, because no sane default exists",
            trigger=_cal(f"condition: {token}"),
        )
        for member, (token, pattern, words) in _DEFECTIVE_FAMILIES.items()
    )
    + tuple(
        _row(
            surface="cal_operator",
            member=member,
            klass=SUPPORTED,
            cite="SEM-37, DL-60",
            effect=effect,
            trigger=_cal(line),
        )
        for member, (effect, line) in _CAL_OPERATORS.items()
    )
    + (
        _row(
            surface="cal_operator",
            member=",",
            facet="list-union",
            klass=PROVISIONAL,
            cite="SEM-37, DL-59",
            label="Q8d",
            sites=("autocal._exclusion_base#1",),
            protocol=_CAL_PROTOCOL,
            effect="the rules of one calendar union; an exclusion-only rule subtracts from"
            " that union",
            trigger=_cal("condition: MON,TUE"),
            quiet=_cal("condition: MON"),
        ),
        _row(
            surface="cal_operator",
            member="&",
            facet="flat-precedence",
            klass=PROVISIONAL,
            cite="SEM-37, DL-59",
            label="Q8d",
            sites=("autocal._parse_rule#1",),
            protocol=_CAL_PROTOCOL,
            effect="& and | evaluate flat left-to-right, with no precedence between them",
            trigger=_cal("condition: MON & JAN | TUE"),
            quiet=_cal("condition: MON"),
        ),
        _row(
            surface="cal_operator",
            member="|",
            facet="flat-precedence",
            klass=PROVISIONAL,
            cite="SEM-37, DL-59",
            label="Q8d",
            # the SAME pinned choice as `&#flat-precedence`, whose row owns
            # the `_parse_rule` marker; a sibling facet claims no site
            protocol=_CAL_PROTOCOL,
            effect="& and | evaluate flat left-to-right, with no precedence between them",
            trigger=_cal("condition: MON | JAN & TUE"),
            quiet=_cal("condition: MON"),
        ),
        _row(
            surface="cal_operator",
            member="and",
            facet="word-synonym",
            klass=SUPPORTED,
            cite="SEM-37, DL-58",
            effect="AND is an exact synonym of &; the word form was verified, unlike OR",
            trigger=_cal("condition: MON AND JAN"),
            quiet=_cal("condition: MON & JAN"),
        ),
        _row(
            surface="cal_operator",
            member="or",
            facet="word-synonym",
            klass=PROVISIONAL,
            cite="SEM-37, DL-59",
            label="Q8d",
            sites=("autocal.<module>#1",),
            protocol=_CAL_PROTOCOL,
            effect="OR is pinned as an exact synonym of |",
            trigger=_cal("condition: MON OR TUE"),
            quiet=_cal("condition: MON | TUE"),
        ),
    )
    + tuple(
        _row(
            surface="cal_action",
            member=f"{category}:{code}",
            klass=SUPPORTED,
            cite="SEM-38",
            effect=_ACTION_EFFECTS[(category, code)],
            trigger=_cal("condition: DAILY", f"{category}: {code.upper()}", holcal=True),
            quiet=_cal("condition: DAILY", holcal=True),
        )
        for category in ("non_workday", "holiday")
        for code in ("o", "s", "n", "w", "p")
    )
    + (
        _row(
            surface="cal_action",
            member="non_workday:n",
            facet="target-recheck",
            klass=PROVISIONAL,
            cite="SEM-38, DL-59",
            label="Q8c",
            sites=(
                "autocal.CompiledCalendar._replace#1",
                "autocal.CompiledCalendar._replace#2",
            ),
            protocol=_CAL_PROTOCOL,
            effect="a replacement target is final: the date-conditions are not"
            " re-checked and a replaced date never re-enters the other category."
            " The categories differ in what they avoid -- a holiday walk skips"
            " holidays, a non_workday walk does not, so non_workday W/P can land on"
            " one",
            trigger=_cal("condition: DAILY", "non_workday: N", "adjust: 1", holcal=True),
            quiet=_cal("condition: DAILY", "non_workday: N", holcal=True),
        ),
    )
)


# ------------------------------------------------------------------ scenarios

_EVENT_SCRIPTS: dict[str, tuple[str, str]] = {
    # member: (effect, the event line that carries the kind)
    "STATUS": (
        "sets a job's status and wakes every job whose condition names it",
        "0 STATUS job=J0 status=SUCCESS",
    ),
    "STARTJOB": (
        "a schedule tick or operator start; it arms must_start whether or not it starts",
        "0 STARTJOB job=J0",
    ),
    "FORCE_STARTJOB": ("starts a job past its condition gate", "0 FORCE_STARTJOB job=J0"),
    "SET_GLOBAL": (
        "sets a global and wakes every job whose condition reads it",
        "0 SET_GLOBAL name=G0 value=1",
    ),
    "ON_ICE": (
        "ices a job: downstream conditions read it as satisfied and it never runs",
        "0 ON_ICE job=J0",
    ),
    "OFF_ICE": ("un-ices a job; conditions are deliberately NOT re-evaluated", "0 OFF_ICE job=J0"),
    "ON_HOLD": ("holds a job: it stays startable but does not start", "0 ON_HOLD job=J0"),
    "OFF_HOLD": ("releases a hold and re-attempts the start immediately", "0 OFF_HOLD job=J0"),
    "ON_NOEXEC": (
        "marks a job as not executing; it completes without running",
        "0 ON_NOEXEC job=J0",
    ),
    "OFF_NOEXEC": ("clears the noexec flag", "0 OFF_NOEXEC job=J0"),
    "DISARM": ("drops a latched tick; no status moves, nothing wakes", "0 DISARM job=J0"),
    "KILLJOB": (
        "terminates a running job, or dequeues and terminates a queued one",
        "0 KILLJOB job=J0",
    ),
    "TIMER": ("a due deadline or deferred start firing off the timer heap", "0 TIMER job=J0"),
}

_STATUS_SCENARIOS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # member: (effect, the jil, the event script)
    "INACTIVE": (
        "the resting status; a live job driven back to it releases everything it held",
        BASE_JIL,
        ("0 STARTJOB job=J0", "1 STATUS job=J0 status=INACTIVE"),
    ),
    "STARTING": (
        "the gate has cleared and the adapter has been handed the run",
        BASE_JIL,
        ("0 STARTJOB job=J0",),
    ),
    "RUNNING": (
        "the command is live and holds whatever capacity it acquired",
        BASE_JIL,
        ("0 STARTJOB job=J0",),
    ),
    "SUCCESS": (
        "a terminal verdict; SEM-09 decides it from the exit code",
        BASE_JIL,
        ("0 STATUS job=J0 status=SUCCESS",),
    ),
    "FAILURE": (
        "a terminal verdict; SEM-09 decides it from the exit code",
        BASE_JIL,
        ("0 STATUS job=J0 status=FAILURE",),
    ),
    "TERMINATED": (
        "a kill that actually happened, never inferred from a missing record",
        BASE_JIL,
        ("0 STARTJOB job=J0", "1 KILLJOB job=J0"),
    ),
    "QUE_WAIT": (
        "the start cleared its condition gate but not its capacity gate",
        _estate(
            "insert_machine: M0\ntype: a\nnode_name: localhost\nmax_load: 1",
            "insert_job: J0\njob_type: c\ncommand: true\nmachine: M0\njob_load: 1",
            "insert_job: J1\njob_type: c\ncommand: true\nmachine: M0\njob_load: 1",
        ),
        ("0 STARTJOB job=J0", "0 STARTJOB job=J1"),
    ),
}

_SLA_JIL = _job(date_conditions="1", start_times='"08:00"', must_start_times='"+30"')
_MC_JIL = _job(date_conditions="1", start_times='"08:00"', must_complete_times='"+20"')

_TIMER_SCENARIOS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "must_start": (
        "armed by the schedule tick; it raises MUST_START_ALARM if no new run began",
        _SLA_JIL,
        ("0 STARTJOB job=J0",),
    ),
    "must_complete": (
        "armed by the start; it raises MUST_COMPLETE_ALARM if the run is still live",
        _MC_JIL,
        ("0 STARTJOB job=J0",),
    ),
    "term_run_time": (
        "armed by the start; it TERMINATEs a run still live at the deadline",
        _job(term_run_time="15"),
        ("0 STARTJOB job=J0",),
    ),
    "deferred_cause": (
        "the fourth timer shape: a run_window-deferred start replaying its own provenance",
        _job(date_conditions="1", days_of_week="all", run_window='"09:00-10:00"'),
        ("0 STARTJOB job=J0",),
    ),
}

SCENARIO_ROWS: tuple[Row, ...] = (
    tuple(
        _row(
            surface="event",
            member=member,
            klass=SUPPORTED,
            cite="ir-design ss7",
            effect=effect,
            trigger=_scn(BASE_JIL, line),
        )
        for member, (effect, line) in _EVENT_SCRIPTS.items()
    )
    + (
        _row(
            surface="event",
            member="MUST_START_ALARM",
            klass=SUPPORTED,
            cite="SEM-34",
            effect="emitted when a must_start deadline passes with no new run; no status moves",
            trigger=_scn(
                _estate(_SLA_JIL, TICKER_BLOCK),
                "0 ON_HOLD job=J0",
                "0 STARTJOB job=J0",
                "31 STATUS job=TICK status=SUCCESS",
            ),
        ),
        _row(
            surface="event",
            member="MUST_COMPLETE_ALARM",
            klass=SUPPORTED,
            cite="SEM-34",
            effect="emitted when a must_complete deadline passes with the run still live",
            trigger=_scn(
                _estate(_MC_JIL, TICKER_BLOCK),
                "0 STARTJOB job=J0",
                "21 STATUS job=TICK status=SUCCESS",
            ),
        ),
        _row(
            surface="event",
            member="ON_ICE",
            facet="armed",
            klass=PROVISIONAL,
            cite="SEM-20, oracle.Oracle._handle_oob",
            label="Q3d",
            sites=("oracle.Oracle._handle_oob#1",),
            protocol="Q3d",
            effect="a pre-existing arm survives the ice round trip untouched",
            trigger=_scn(BASE_JIL, "0 ON_ICE job=J0", "1 OFF_ICE job=J0"),
            quiet=_scn(BASE_JIL, "0 ON_HOLD job=J0", "1 OFF_HOLD job=J0"),
        ),
        _row(
            surface="event",
            member="ON_ICE",
            facet="running",
            klass=PROVISIONAL,
            cite="SEM-05, SEM-20, DL-13, oracle.Oracle._atom_true",
            effect="icing a STARTING or RUNNING job does NOT make its atoms read as"
            " satisfied: the in-flight run is real, so conditions keep reading the live"
            " status until it completes (DL-13); no label was opened for the exception",
            trigger=_scn(BASE_JIL, "0 STARTJOB job=J0", "1 ON_ICE job=J0"),
            quiet=_scn(BASE_JIL, "0 ON_ICE job=J0"),
        ),
        _row(
            surface="event",
            member="ON_ICE",
            facet="queued",
            klass=PROVISIONAL,
            cite="DL-50, oracle.Oracle._handle_oob",
            label="Qr5",
            effect="icing a queued job dequeues it and settles it INACTIVE now, rather than"
            " leaving it in QUE_WAIT",
            trigger=_scn(
                _STATUS_SCENARIOS["QUE_WAIT"][1],
                "0 STARTJOB job=J0",
                "0 STARTJOB job=J1",
                "1 ON_ICE job=J1",
            ),
            quiet=_scn(BASE_JIL, "0 ON_ICE job=J0"),
        ),
        _row(
            surface="event",
            member="KILLJOB",
            facet="queued",
            klass=PROVISIONAL,
            cite="DL-50, oracle.Oracle._dispatch",
            label="Qr5",
            effect="killing a queued job dequeues it, consumes its arm and TERMINATEs it",
            trigger=_scn(
                _STATUS_SCENARIOS["QUE_WAIT"][1],
                "0 STARTJOB job=J0",
                "0 STARTJOB job=J1",
                "1 KILLJOB job=J1",
            ),
            quiet=_scn(BASE_JIL, "0 KILLJOB job=J0"),
        ),
    )
    + tuple(
        _row(
            surface="status",
            member=member,
            klass=SUPPORTED,
            cite="ir-design ss7",
            effect=effect,
            trigger=_scn(jil, *events),
        )
        for member, (effect, jil, events) in _STATUS_SCENARIOS.items()
    )
    + tuple(
        _row(
            surface="timer",
            member=member,
            klass=SUPPORTED,
            cite="PR-09, oracle.Oracle._schedule_timer",
            effect=effect,
            trigger=_scn(jil, *events),
        )
        for member, (effect, jil, events) in _TIMER_SCENARIOS.items()
    )
)


# ------------------------------------------------------------------ profile

_PROFILE_FIELDS: dict[str, tuple[str, str, str]] = {
    # member: (cite, effect, the override JSON)
    "default_tz": (
        "period-model ss2.1, SEM-35",
        "the zone a job with no timezone of its own is read in",
        '{"default_tz": "Europe/Berlin"}',
    ),
    "tz_aliases": (
        "period-model ss2.1, DL-62",
        "the site-local zone-name table; a name only it resolves fails without it",
        '{"tz_aliases": {"EST5EDT": "America/New_York"}}',
    ),
    "as_machine": (
        "period-model ss2.1, DL-52",
        "the machine names this runner answers to, sorted and de-duplicated",
        '{"as_machine": ["greezy_spoon"]}',
    ),
    "machine_policy": (
        "period-model ss2.1, DL-49",
        "how the one ambiguous machine verdict resolves",
        '{"machine_policy": "local-eligible"}',
    ),
    "execution_mode": (
        "period-model ss2.1",
        "whether the engine owns the child processes or a supervisor does",
        '{"execution_mode": "detached"}',
    ),
    "deadman_us": (
        "period-model ss2.1, DL-126",
        "the supervisor's observed deadman interval; null means there is no deadman",
        '{"deadman_us": 30000000}',
    ),
    "fw_default_interval_us": (
        "period-model ss2.1",
        "the poll interval an FW job with no watch_interval uses",
        '{"fw_default_interval_us": 30000000}',
    ),
    "cmd_grace_us": (
        "period-model ss2.1",
        "the grace between SIGTERM and SIGKILL on a cancelled command",
        '{"cmd_grace_us": 5000000}',
    ),
    "reconcile_settle_us": (
        "period-model ss2.1",
        "how long reconcile waits for late evidence before it decides",
        '{"reconcile_settle_us": 1000000}',
    ),
    "spawn_window_us": (
        "period-model ss2.1",
        "the window a spawn has to produce its receipt",
        '{"spawn_window_us": 1000000}',
    ),
    "retry_horizon_us": (
        "period-model ss2.1",
        "how far ahead a deferred dispatch retry may be scheduled",
        '{"retry_horizon_us": 30000000}',
    ),
}

_PROFILE_FACETS: tuple[Row, ...] = (
    _row(
        surface="profile_field",
        member="fw_default_interval_us",
        facet="rounding",
        klass=PROVISIONAL,
        cite="runner_startup.wire_from_profile, period-model ss2.1",
        effect="startup converts the microsecond profile field to WHOLE SECONDS for the"
        " watcher and clamps it to at least one, so a sub-second interval is not what"
        " the profile asked for. No label was opened for the conversion",
        trigger='{"fw_default_interval_us": 500000}',
        quiet='{"fw_default_interval_us": 30000000}',
    ),
)

_PROFILE_ALTS: dict[str, str] = {
    "machine_policy=strict": "a job whose machine does not resolve local is refused",
    "machine_policy=local-eligible": "only a MIXED pool runs here, with a warning that"
    " pool placement was ignored; a foreign or unreadable machine still refuses",
    "execution_mode=tethered": "the engine owns the child processes; there is no supervisor",
    "execution_mode=detached": "a supervisor owns the child processes across engine restarts",
}

PROFILE_ROWS: tuple[Row, ...] = (
    tuple(
        _row(
            surface="profile_field",
            member=member,
            klass=SUPPORTED,
            cite=cite,
            effect=effect,
            trigger=override,
        )
        for member, (cite, effect, override) in _PROFILE_FIELDS.items()
    )
    + tuple(
        _row(
            surface="profile_alt",
            member=member,
            klass=SUPPORTED,
            cite="period-model ss2.1",
            effect=effect,
            trigger='{"%s": "%s"}' % tuple(member.split("=", 1)),
        )
        for member, effect in _PROFILE_ALTS.items()
    )
    + _PROFILE_FACETS
)


# ------------------------------------------------------------------ adapters

#: An `outcome` fixture NAMES a result shape; nothing executes it. The
#: detector is string equality, and the proof that these shapes are the ones
#: the code can build is the AST derivation, not the fixture (S2's collector
#: is what will observe one).
#: Every `Failed(` / `Terminated(` template the adapter layer can build, as
#: the test derives them: a constant is itself, an f-string is its constant
#: parts with `{}` where a value goes, and anything else is `<dynamic:...>`
#: qualified by the function that builds it (DL-209, R-a).
_UNOBSERVABLE = "exit_status_unobservable"

#: Which E7 site each unobservable-exit template owns: the bare cause is the
#: resume ladder's, the rc-bearing one belongs to the two live adapters.
_E7_SITES: dict[str, tuple[str, ...]] = {
    f"Failed={_UNOBSERVABLE}": (
        "runner_adapters.resolve_spool#1",
        "runner_startup.<module>#1",
    ),
    f"Failed={_UNOBSERVABLE} (wrapper exited rc={{}} without a status record)": (
        "runner_adapters.LocalCommandAdapter.run#1",
        "runner_adapters.SupervisedCommandAdapter._await_outcome#1",
    ),
}
_CRASH_CAUSE = "dispatch lost to engine crash (run directory missing)"

_OUTCOME_TEMPLATES: dict[str, tuple[str, str, str]] = {
    # "Kind=template": (class, cite, effect)
    f"Failed={_UNOBSERVABLE}": (
        PROVISIONAL,
        "runner_adapters.resolve_spool, runner-design ss15",
        "a resumed run with no status record fails rather than guessing an exit code",
    ),
    f"Failed={_UNOBSERVABLE} (wrapper exited rc={{}} without a status record)": (
        PROVISIONAL,
        "runner_adapters.LocalCommandAdapter.run,"
        " runner_adapters.SupervisedCommandAdapter._await_outcome, runner-design ss15",
        "a wrapper that exited without writing a status record fails the run and"
        " names the wrapper's own exit code; both the tethered and the supervised"
        " adapter build it",
    ),
    f"Failed={_CRASH_CAUSE}": (
        SUPPORTED,
        "runner_adapters.resolve_spool, DL-118",
        "a dispatch whose run directory is gone provably never reached the host,"
        " so it fails rather than being retried blind",
    ),
    "Failed=malformed status record: outcome 'exited' with exit_code={}": (
        REFUSED,
        "runner_adapters.outcome_from_status",
        "an 'exited' record with no integer exit code is refused as a truthful"
        " FAILURE, never mapped to something a downstream success could consume",
    ),
    "Failed=unrecognized status record outcome {}": (
        REFUSED,
        "runner_adapters.outcome_from_status",
        "a status record whose outcome the protocol does not define is refused, never guessed",
    ),
    "Failed=spawn failed: {}": (
        SUPPORTED,
        "runner_adapters.outcome_from_status",
        "the wrapper recorded that the spawn itself failed; the run never started",
    ),
    "Failed=wrapper spawn failed: {}": (
        SUPPORTED,
        "runner_adapters.LocalCommandAdapter.run, runner_adapters.SupervisedCommandAdapter.run",
        "the engine could not spawn the wrapper at all; the run never started",
    ),
    "Terminated=<dynamic:outcome_from_status>": (
        SUPPORTED,
        "runner_adapters.outcome_from_status, DL-41a",
        "a signalled or terminated status record carries its own cause text into"
        " the TERMINATED verdict",
    ),
    "Terminated=wrapper lost; killed at resume": (
        SUPPORTED,
        "runner_adapters.resolve_spool",
        "a resume that finds the wrapper gone kills the surviving command group"
        " and reports the kill that happened",
    ),
}

ADAPTER_ROWS: tuple[Row, ...] = (
    _row(
        surface="adapter_outcome",
        member="int",
        klass=SUPPORTED,
        cite="runner-design ss6, SEM-09",
        effect="a raw exit code; the SUCCESS/FAILURE verdict over it stays oracle-side",
        trigger="int",
        quiet="Terminated",
    ),
    _row(
        surface="adapter_outcome",
        member="Terminated",
        klass=SUPPORTED,
        cite="runner-design ss6, DL-41a",
        effect="an OBSERVED kill; the engine injects STATUS TERMINATED for it",
        trigger="Terminated",
    ),
    _row(
        surface="adapter_outcome",
        member="Failed",
        klass=SUPPORTED,
        cite="runner-design ss6",
        effect="a completion with no raw exit code; the engine injects STATUS FAILURE"
        " with the cause",
        trigger="Failed",
    ),
    *(
        _row(
            surface="adapter_outcome",
            member=member,
            klass=klass,
            cite=cite,
            # the E7 label sits on every unobservable-exit template
            label="E7" if _UNOBSERVABLE in member else None,
            sites=_E7_SITES.get(member, ()),
            effect=effect,
            trigger=member,
        )
        for member, (klass, cite, effect) in _OUTCOME_TEMPLATES.items()
    ),
    _row(
        surface="adapter_outcome",
        member="Terminated",
        facet="external-signal",
        klass=PROVISIONAL,
        cite="runner_adapters",
        label="E8",
        sites=("runner_adapters.outcome_from_status#1",),
        protocol="E8",
        effect="a kill by an external signal is reported as TERMINATED, the same verdict"
        " an oracle-ordered kill gets",
        trigger="Terminated",
        quiet="int",
    ),
    _row(
        surface="adapter_policy",
        member="unscripted-completion",
        klass=SUPPORTED,
        cite="runner_adapters.FakeAdapter",
        effect="a rehearsed run with no script entry completes instantly with exit code 0",
        trigger="int",
        quiet="Failed",
    ),
    _row(
        surface="adapter_policy",
        member="no-retry",
        klass=PASSTHROUGH,
        cite="DL-53",
        effect="the adapter never retries; n_retrys is carried and not applied",
        trigger="Failed",
        quiet="int",
    ),
    _row(
        surface="adapter_policy",
        member="no-timeout",
        klass=SUPPORTED,
        cite="runner_adapters.LocalCommandAdapter",
        effect="the adapter imposes no timeout of its own; term_run_time is the oracle's timer",
        trigger="Terminated",
        quiet="int",
    ),
)


# ------------------------------------------------- engine-side value surfaces

#: A `site` fixture is a `module.qualname` string; the detector parses that
#: function and answers whether it stamps the member. Circularity is bounded
#: on purpose: the fixture names WHERE the provenance is stamped, and the
#: domain is derived from every `source=` constant in the package, so a new
#: provenance with no row still fails.
_SOURCE_SITES: dict[str, tuple[str, str, str]] = {
    # member: (the stamping site, a site that stamps something else, effect)
    "scheduler": (
        "runner.Engine.run_until_quiescent",
        "runner.Engine.inject_host",
        "the start came from a calendar tick, so a journal reader can tell it from"
        " an operator's sendevent; `Engine._cutoff` stamps it on the boundary path",
    ),
    "control": (
        "runner.Engine.inject",
        "runner.Engine._enqueue",
        "the event crossed the ss10 control socket, or a rehearsal script stood in"
        " for one; it is not something the engine raised itself",
    ),
    "reconcile": (
        "runner_startup._inject_completion",
        "runner.Engine._cutoff",
        "the completion came from resolving an incomplete run at resume, not from a"
        " live adapter; it still goes through the ss4 stale gate",
    ),
    "adapter": (
        "runner.Engine._enqueue",
        "runner.Engine._cutoff",
        "the event is a live adapter completion -- the stamp that subjects it to the"
        " ss4 stale gate; it is the DEFAULT provenance of an engine-raised input",
    ),
}

EVENT_SOURCE_ROWS: tuple[Row, ...] = tuple(
    _row(
        surface="event_source",
        member=member,
        klass=SUPPORTED,
        cite=f"ir-design ss7, DL-68, {site}",
        effect=effect,
        trigger=site,
        quiet=other,
    )
    for member, (site, other, effect) in _SOURCE_SITES.items()
)


BOX_ARM_JIL = _estate(
    MACHINE_BLOCK,
    "insert_job: BOX0\njob_type: b",
    "insert_job: MEM\njob_type: c\ncommand: true\nmachine: M0\nbox_name: BOX0\n"
    'condition: s(GATE)\ndate_conditions: 1\ndays_of_week: all\nstart_times: "08:00"',
    "insert_job: GATE\njob_type: c\ncommand: true\nmachine: M0",
)
SLA_START_JIL = _estate(
    _job(date_conditions="1", days_of_week="all", start_times='"08:00"', must_start_times='"+30"'),
    TICKER_BLOCK,
)
SLA_COMPLETE_JIL = _estate(
    _job(
        date_conditions="1", days_of_week="all", start_times='"08:00"', must_complete_times='"+20"'
    ),
    TICKER_BLOCK,
)

#: Every out-of-band marker `Oracle._record` can write: a trace line that is
#: not a status transition. The status surface derives `JobStatus`; this one
#: derives the vocabulary beside it.
_TRACE_MARKERS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # member: (effect, the jil, the event script)
    "ON_ICE": (
        "the job is iced: downstream conditions read it as satisfied and it never runs",
        BASE_JIL,
        ("0 ON_ICE job=J0",),
    ),
    "OFF_ICE": (
        "the ice is cleared; conditions are deliberately NOT re-evaluated",
        BASE_JIL,
        ("0 ON_ICE job=J0", "1 OFF_ICE job=J0"),
    ),
    "ON_HOLD": (
        "the job is held: it stays startable but no start goes through",
        BASE_JIL,
        ("0 ON_HOLD job=J0",),
    ),
    "OFF_HOLD": (
        "the hold is released and the start is re-attempted immediately",
        BASE_JIL,
        ("0 ON_HOLD job=J0", "1 OFF_HOLD job=J0"),
    ),
    "ON_NOEXEC": (
        "the job is marked not-executing; it completes without running",
        BASE_JIL,
        ("0 ON_NOEXEC job=J0",),
    ),
    "OFF_NOEXEC": (
        "the noexec flag is cleared",
        BASE_JIL,
        ("0 ON_NOEXEC job=J0", "1 OFF_NOEXEC job=J0"),
    ),
    "DISARM": (
        "an explicit journaled disarm: the latched tick is dropped and nothing else moves",
        BASE_JIL,
        ("0 DISARM job=J0",),
    ),
    "START_REFUSED": (
        "a start request the oracle declined, with the reason it declined it",
        BASE_JIL,
        ("0 STARTJOB job=J0", "1 STARTJOB job=J0"),
    ),
    "SCHED_ARM": (
        "a schedule tick that could not start the job latched instead",
        SLA_START_JIL,
        ("0 ON_HOLD job=J0", "0 STARTJOB job=J0"),
    ),
    "SCHED_DISARM": (
        "an unconsumed member arm died with the box run that armed it",
        BOX_ARM_JIL,
        ("0 STARTJOB job=BOX0", "1 STARTJOB job=MEM", "2 STATUS job=BOX0 status=SUCCESS"),
    ),
    "MUST_START_ALARM": (
        "the must_start deadline passed with no new run; no status moved",
        SLA_START_JIL,
        ("0 ON_HOLD job=J0", "0 STARTJOB job=J0", "31 STATUS job=TICK status=SUCCESS"),
    ),
    "MUST_COMPLETE_ALARM": (
        "the must_complete deadline passed with the run still live; no status moved",
        SLA_COMPLETE_JIL,
        ("0 STARTJOB job=J0", "21 STATUS job=TICK status=SUCCESS"),
    ),
    "RUN_WINDOW_DEFER": (
        "a start outside the run_window, closer to the next opening, was queued for it",
        _job(date_conditions="1", days_of_week="all", run_window='"09:00-10:00"'),
        ("0 STARTJOB job=J0",),
    ),
    "RUN_WINDOW_SKIP": (
        "a start outside the run_window, closer to the previous close, was dropped",
        _job(date_conditions="1", days_of_week="all", run_window='"06:00-07:00"'),
        ("0 STARTJOB job=J0",),
    ),
}

TRACE_MARKER_ROWS: tuple[Row, ...] = tuple(
    _row(
        surface="trace_marker",
        member=member,
        klass=SUPPORTED,
        cite="ir-design ss7, oracle.Oracle._record",
        effect=effect,
        trigger=_scn(jil, *events),
    )
    for member, (effect, jil, events) in _TRACE_MARKERS.items()
)


FOREIGN_MACHINE = "insert_machine: FAR\ntype: a\nnode_name: otherhost"
LOCAL_MACHINE = "insert_machine: LOC\ntype: a\nnode_name: localhost"
POOL_MACHINE = "insert_machine: POOL\ntype: v\nmachine: LOC\nmachine: FAR"


def _placed(machine: str, *blocks: str) -> str:
    """An estate whose one job is placed on `machine`."""
    return _estate(
        MACHINE_BLOCK,
        *blocks,
        f"insert_job: J0\njob_type: c\ncommand: true\nmachine: {machine}",
    )


LOCAL_ESTATE = _placed("LOC", LOCAL_MACHINE)
FOREIGN_ESTATE = _placed("FAR", FOREIGN_MACHINE)

#: Preflight's own verdict vocabulary. Fixtures here deliberately produce
#: preflight findings -- that is what the surface enumerates.
_PREFLIGHT_CODES: dict[str, tuple[str, str, str, str]] = {
    # member: (class, cite, effect, the estate)
    "resources": (
        REFUSED,
        "runner_preflight._resource_preflight, DL-50",
        "a resource the oracle cannot model faithfully refuses the run",
        _job("insert_resource: R0\nres_type: R", resources="(R0, QUANTITY=1)"),
    ),
    "owner": (
        REFUSED,
        "runner_preflight._owner_preflight",
        "an owner other than the invoking user refuses the run: there is no setuid",
        _job(owner="someone_else"),
    ),
    "machine": (
        REFUSED,
        "runner_preflight._machine_preflight, DL-49",
        "a job whose machine does not resolve to this host refuses the run:"
        " there is no remote fabric",
        FOREIGN_ESTATE,
    ),
    "machine-mixed": (
        SUPPORTED,
        "runner_preflight._machine_preflight, DL-49",
        "a pool with some members here and some elsewhere runs here under"
        " local-eligible, with a warning that pool placement was ignored",
        _placed("POOL", LOCAL_MACHINE, FOREIGN_MACHINE, POOL_MACHINE),
    ),
    "calendar": (
        REFUSED,
        "runner_preflight._calendar_preflight, DL-56",
        "a calendar the scheduler cannot read or that can never fire refuses the run",
        _job(date_conditions="1", run_calendar="MISSING"),
    ),
    "timezone": (
        REFUSED,
        "runner_preflight._timezone_preflight, SEM-35",
        "a timezone name the SEM-35 ladder cannot resolve refuses the run",
        _job(
            date_conditions="1",
            days_of_week="all",
            start_times='"08:00"',
            timezone="Mars/Olympus",
        ),
    ),
    "n-retrys": (
        SUPPORTED,
        "runner_preflight._retry_preflight, DL-53",
        "the run is warned, not refused: n_retrys is carried and never applied,"
        " so the job runs exactly once",
        _job(n_retrys="2"),
    ),
    "skeleton-cycle": (
        SUPPORTED,
        "runner_preflight._skeleton_cycle_preflight, DL-13",
        "a cycle in the AND-success skeleton is legal AutoSys; it warns and"
        " disables `plan` rather than refusing the run",
        _estate(
            MACHINE_BLOCK,
            "insert_job: A\njob_type: c\ncommand: true\nmachine: M0\ncondition: s(B)",
            "insert_job: B\njob_type: c\ncommand: true\nmachine: M0\ncondition: s(A)",
        ),
    ),
}

#: The codes above, plus the two no JIL can reach. An unreachable row says so
#: on the row (`reachable=False`); both its fixtures are ordinary estates that
#: show the gate passing, so it names its quiet explicitly -- the base estate
#: is already its trigger, and a row's two fixtures must differ
#: (DL-75 review 2026-09-19).
PREFLIGHT_CODE_ROWS: tuple[Row, ...] = tuple(
    _row(
        surface="preflight_code",
        member=member,
        klass=klass,
        cite=cite,
        effect=effect,
        trigger=estate,
    )
    for member, (klass, cite, effect, estate) in _PREFLIGHT_CODES.items()
) + (
    _row(
        surface="preflight_code",
        member="job-type",
        klass=REFUSED,
        cite="runner_preflight._job_type_preflight",
        reachable=False,
        effect="a job_type with no adapter refuses the run; no JIL reaches this gate,"
        " because lowering already refuses every type outside CMD/BOX/FW",
        trigger=BASE_JIL,
        quiet=GLOBAL_JIL,
    ),
    _row(
        surface="preflight_code",
        member="oracle",
        klass=REFUSED,
        cite="runner_preflight._oracle_preflight",
        reachable=False,
        effect="an oracle that will not construct over this catalog refuses the run;"
        " no JIL reaches this gate, because lowering builds no such catalog",
        trigger=BASE_JIL,
        quiet=GLOBAL_JIL,
    ),
)


#: What each mode does to the bucket, per `capacity.requirement_demand`.
_DEMAND_EFFECTS = {
    "acquire": "the start HOLDS its units until the release policy gives them back;"
    " a bucket short of them queues the job in QUE_WAIT",
    "gate": "a threshold check only (res_type T): the level is read, nothing is held"
    " and so nothing is ever released",
}

DEMAND_ROWS: tuple[Row, ...] = tuple(
    _row(
        surface="demand_mode",
        member=member,
        klass=SUPPORTED,
        cite="DL-50, capacity.requirement_demand",
        effect=effect,
        trigger=_job(
            f"insert_resource: R0\nres_type: {'T' if member == 'gate' else 'R'}\namount: 4",
            resources="(R0, QUANTITY=1)",
        ),
    )
    for member, effect in _DEMAND_EFFECTS.items()
)

MACHINE_VERDICT_ROWS: tuple[Row, ...] = (
    _row(
        surface="machine_verdict",
        member="local",
        klass=SUPPORTED,
        cite="DL-49, DL-52",
        effect="the job's machine resolves to a name this runner answers to, so it runs here",
        trigger=LOCAL_ESTATE,
        quiet=FOREIGN_ESTATE,
    ),
    _row(
        surface="machine_verdict",
        member="foreign",
        klass=SUPPORTED,
        cite="DL-49, DL-52",
        effect="the job's machine resolves elsewhere; the verdict is modelled and"
        " `preflight_code:machine` is the ERROR it becomes -- one behaviour, read"
        " once as a verdict and once as a refusal",
        trigger=FOREIGN_ESTATE,
        quiet=LOCAL_ESTATE,
    ),
    _row(
        surface="machine_verdict",
        member="mixed",
        klass=SUPPORTED,
        cite="DL-49",
        effect="a pool with members on both sides; the machine policy decides whether it runs here",
        trigger=_placed("POOL", LOCAL_MACHINE, FOREIGN_MACHINE, POOL_MACHINE),
        quiet=LOCAL_ESTATE,
    ),
    _row(
        surface="machine_verdict",
        member="error",
        klass=REFUSED,
        cite="runner_preflight.resolve_machine, DL-49",
        effect="a machine definition the resolver cannot read -- no type, an empty pool,"
        " a nested or undefined member -- is refused, never guessed",
        trigger=_placed("BADPOOL", "insert_machine: BADPOOL\ntype: v"),
        quiet=LOCAL_ESTATE,
    ),
)


# ----------------------------------------------- wrapper, calendar and literal forms

_WRAPPER_OUTCOMES: dict[str, tuple[str, str, str]] = {
    # member: (class, cite, effect)
    "exited": (
        SUPPORTED,
        "runner-design ss6, supervisor-protocol ss3, SEM-09",
        "the command ended on its own; the raw exit code goes to the oracle and"
        " SEM-09 decides the verdict",
    ),
    "signaled": (
        SUPPORTED,
        "runner-design ss6, DL-41a",
        "the command was killed by a signal; the engine injects STATUS TERMINATED"
        " because a kill actually happened",
    ),
    "terminated": (
        SUPPORTED,
        "runner-design ss6, DL-41a",
        "the wrapper killed the command when it lost its parent; the engine injects"
        " STATUS TERMINATED with the recorded cause",
    ),
    "spawn_failed": (
        SUPPORTED,
        "runner-design ss6",
        "/bin/sh could never be spawned; the engine injects STATUS FAILURE and the"
        " run never started",
    ),
}

WRAPPER_OUTCOME_ROWS: tuple[Row, ...] = tuple(
    _row(
        surface="wrapper_outcome",
        member=member,
        klass=klass,
        cite=cite,
        effect=effect,
        trigger=f"wrapper={member}",
    )
    for member, (klass, cite, effect) in _WRAPPER_OUTCOMES.items()
) + (
    _row(
        surface="wrapper_outcome",
        member="exited",
        facet="no-exit-code",
        klass=REFUSED,
        cite="runner_adapters.outcome_from_status",
        effect="an 'exited' record whose exit_code is not an integer is refused as a"
        " truthful FAILURE, never mapped to anything a success-dependent downstream"
        " could consume",
        trigger="wrapper=exited",
        quiet="wrapper=signaled",
    ),
)


CAL_FORM_ROWS: tuple[Row, ...] = (
    _row(
        surface="cal_workday_form",
        member="all",
        klass=SUPPORTED,
        cite="SEM-36, DL-60",
        effect="the observed `all` serialization makes every day of the week a workday",
        trigger=_cal("condition: DAILY", "workday: all"),
        quiet=_cal("condition: DAILY", "workday: xxxxx.."),
    ),
    _row(
        surface="cal_workday_form",
        member="mask",
        klass=SUPPORTED,
        cite="SEM-36",
        effect="the positional seven-character `{X|.}` mask reads Monday first",
        trigger=_cal("condition: DAILY", "workday: xxxxx.."),
        quiet=_cal("condition: DAILY", "workday: all"),
    ),
    _row(
        surface="cal_workday_form",
        member="codes",
        klass=SUPPORTED,
        cite="SEM-36",
        effect="a comma list of two- or three-letter day codes; it is also the"
        " fallthrough form, so an unrecognized day is refused here",
        trigger=_cal("condition: DAILY", "workday: mo,tu,we,th,fr"),
        quiet=_cal("condition: DAILY", "workday: all"),
    ),
    _row(
        surface="cal_row_form",
        member="date",
        klass=SUPPORTED,
        cite="SEM-36, DL-58",
        effect="a bare date row fires at 00:00, the vendor's firing time for a job with"
        " no start_times of its own",
        trigger=_stmt("calendar: SC0\n01/01/2026"),
    ),
    _row(
        surface="cal_row_form",
        member="hh:mm",
        klass=SUPPORTED,
        cite="SEM-36",
        effect="a minute-grained time tail becomes the row's tick",
        trigger=_stmt("calendar: SC0\n01/01/2026 08:30"),
    ),
    _row(
        surface="cal_row_form",
        member="hh:mm:ss",
        klass=SUPPORTED,
        cite="SEM-36, DL-60",
        effect="the observed export's seconds tail is accepted and truncated to the"
        " minute, because ticks are minute-grained",
        trigger=_stmt("calendar: SC0\n01/01/2026 08:30:45"),
    ),
)


#: Every closed alternative set a Literal declares in the estate-facing
#: modules that no dedicated surface already owns. The fixture kind is
#: `site`: most of these are discriminators and shapes a JIL estate cannot
#: select directly, so the row names the declaring module instead.
_LITERAL_ALTS: dict[str, tuple[str, str, str]] = {
    # member: (cite, effect, the declaring site)
    "And.kind=and": (
        "SEM-03, ir-design ss3",
        "the discriminator that makes an AND node readable back from JSON",
        "conditions.And",
    ),
    "Or.kind=or": (
        "SEM-03, ir-design ss3",
        "the discriminator that makes an OR node readable back from JSON",
        "conditions.Or",
    ),
    "Paren.kind=paren": (
        "SEM-03, ir-design ss3",
        "the discriminator that keeps explicit grouping in the model",
        "conditions.Paren",
    ),
    "StatusAtom.kind=status": (
        "SEM-02, ir-design ss3",
        "the discriminator of a job-status atom",
        "conditions.StatusAtom",
    ),
    "ExitCodeAtom.kind=exitcode": (
        "SEM-02, ir-design ss3",
        "the discriminator of an exit-code atom",
        "conditions.ExitCodeAtom",
    ),
    "GlobalAtom.kind=global": (
        "SEM-08, ir-design ss3",
        "the discriminator of a global-variable atom",
        "conditions.GlobalAtom",
    ),
    "ExecSpec.kind=cmd": (
        "SEM-10, ir-design ss4",
        "the discriminator that selects the command exec spec",
        "ir.ExecSpec",
    ),
    "FwSpec.kind=fw": (
        "SEM-10, ir-design ss4",
        "the discriminator that selects the file-watcher exec spec",
        "ir.FwSpec",
    ),
    "CalendarIR.kind=standard": (
        "SEM-36, DL-36",
        "a calendar of date rows; `standard_days` reads it and `holcal` requires it",
        "ir.CalendarIR",
    ),
    "CalendarIR.kind=extended": (
        "SEM-36, DL-36",
        "a calendar of rules; `compile_calendar` reads it and refuses a standard one",
        "ir.CalendarIR",
    ),
    "CatalogIR.ir_version=0.2": (
        "ir-design ss4",
        "the IR version stamped on every catalog; a reader that meets another refuses",
        "ir.CatalogIR",
    ),
    "SlaSpec.kind=absolute": (
        "SEM-34, oracle.Oracle._arm_sla_and_term",
        "an absolute must_*_times is lowered and carried, and arms nothing: the oracle"
        " owns no calendar, so no absolute deadline exists v1",
        "ir.SlaSpec",
    ),
    "SlaSpec.kind=relative": (
        "SEM-34, oracle.Oracle._arm_sla_and_term",
        "a relative `+n` must_*_times is what arms the alarm timer",
        "ir.SlaSpec",
    ),
    "PreflightItem.severity=ERROR": (
        "runner-design ss8",
        "the finding refuses the run",
        "runner_preflight.PreflightItem",
    ),
    "PreflightItem.severity=WARN": (
        "runner-design ss8",
        "the finding is printed and journaled, and the run goes ahead",
        "runner_preflight.PreflightItem",
    ),
    "ResolvedTz.how=os": (
        "SEM-35",
        "the zone name resolved straight out of the OS database",
        "timezones.ResolvedTz",
    ),
    "ResolvedTz.how=map": (
        "SEM-35, DL-62",
        "the name resolved through the estate's ujo_timezones alias table, chained at"
        " most five hops with an OS lookup per hop",
        "timezones.ResolvedTz",
    ),
    "ResolvedTz.how=city": (
        "SEM-35",
        "the unique-city default, which applies ONLY when the estate supplied no alias"
        " table at all",
        "timezones.ResolvedTz",
    ),
    "ResolvedTz.how=posix": (
        "SEM-35",
        "a POSIX fixed-offset spelling, resolved without the zone database",
        "timezones.ResolvedTz",
    ),
}

LITERAL_ALT_ROWS: tuple[Row, ...] = tuple(
    _row(
        surface="literal_alt",
        member=member,
        klass=SUPPORTED,
        cite=cite,
        effect=effect,
        trigger=site,
        # a site in ANOTHER module: the detector reads what the module
        # declares, so the quiet has to be somewhere that declares none of it
        quiet="conditions.parse_condition" if site.startswith("ir.") else "ir.unquote_jil_value",
    )
    for member, (cite, effect, site) in _LITERAL_ALTS.items()
)


# ------------------------------------------------------------------ runtime

RUNTIME_ROWS: tuple[Row, ...] = (
    _row(
        surface="runtime",
        member="member-arm-scope",
        klass=PROVISIONAL,
        cite="oracle.Oracle._after_transition",
        label="Q3c",
        sites=("oracle.<module>#1", "oracle.Oracle._after_transition#1"),
        protocol="Q3c",
        effect="a box member's latched tick is scoped to the box run it was latched in",
        trigger=_job(BOX_BLOCK, box_name="BOX0", condition="s(BOX0)"),
        quiet=BASE_JIL,
    ),
    _row(
        surface="runtime",
        member="missed-tick-skip",
        klass=PROVISIONAL,
        cite="runner_startup, runner_scheduler.Scheduler.pop_due",
        label="E9",
        sites=(
            "runner_scheduler.<module>#1",
            "runner_scheduler.Scheduler.pop_due#1",
            "runner_startup._resume_under_lock#1",
            "runner_startup.resume_run#1",
        ),
        effect="a tick whose instant passed while the engine was down is journaled and"
        " dropped, never fired late",
        trigger=SCHEDULED_JOB,
        quiet=BASE_JIL,
    ),
    _row(
        surface="runtime",
        member="exclusion-only-compound",
        klass=PROVISIONAL,
        cite="autocal.compile_calendar, DL-59",
        label="Q8d",
        sites=(
            "autocal.compile_calendar#1",
            "autocal.compile_calendar#2",
        ),
        protocol=_CAL_PROTOCOL,
        effect="a compound rule with no inclusive leaf is evaluated literally as an include,"
        " which makes it near-universal",
        trigger=_cal("condition: XTUE|XWED"),
        quiet=_cal("condition: DAILY"),
    ),
    _row(
        surface="runtime",
        member="scan-horizon",
        klass=SUPPORTED,
        cite="autocal._SCAN_YEARS, runner_scheduler._EXTENDED_SCAN_DAYS",
        bound="60 years",
        effect="a calendar that generates nothing within 60 years reads as exhausted;"
        " dormancy is proven within that bound only",
        trigger=_cal("condition: FEB#29 & MON"),
        quiet=_cal("condition: DAILY"),
    ),
    _row(
        surface="runtime",
        member="walk-cap",
        klass=REFUSED,
        cite="autocal.CompiledCalendar._walk",
        bound="366 days",
        effect="a W/P replacement that finds no valid day within 366 days is degenerate"
        " and refuses the calendar",
        trigger=_estate(
            MACHINE_BLOCK,
            JOB_BLOCK,
            "extended_calendar: EC0\ncondition: DAILY\nworkday: x......\n"
            f"holiday: W\nholcal: {HOLCAL_NAME}",
            STARVED_HOLCAL_BLOCK,
        ),
        quiet=_estate(
            MACHINE_BLOCK,
            JOB_BLOCK,
            "extended_calendar: EC0\ncondition: DAILY\nworkday: x......\n"
            f"holiday: S\nholcal: {HOLCAL_NAME}",
            STARVED_HOLCAL_BLOCK,
        ),
    ),
    _row(
        surface="runtime",
        member="empty-workday-mask",
        klass=REFUSED,
        cite="autocal.compile_calendar",
        effect="a W/P action with an all-non-workday mask has nowhere to walk and"
        " refuses the calendar before any day is generated",
        trigger=_cal("condition: DAILY", "non_workday: W", "workday: ......."),
        quiet=_cal("condition: DAILY", "non_workday: W", "workday: x......"),
    ),
    _row(
        surface="runtime",
        member="calendar-preflight-candidates",
        klass=SUPPORTED,
        cite="DL-57, runner_preflight._next_eligible_day",
        bound="732 candidates",
        effect="the eligible-day probe advances at most 732 candidates, not 732 days:"
        " a sparse calendar's 732 candidates can span decades, so the bound is on"
        " what was examined and not on the time it covered",
        trigger=_job(HOLCAL_BLOCK, date_conditions="1", run_calendar=f'"{HOLCAL_NAME}"'),
        quiet=_job(date_conditions="1", days_of_week="all"),
    ),
    _row(
        surface="runtime",
        member="preflight-date-basis-utc",
        klass=SUPPORTED,
        cite="DL-212, runner_preflight._preflight_local_day",
        effect="the calendar probes read the run anchor on the scheduler's own ladder:"
        " the JOB's local day, else the run-level base timezone, else UTC. Preflight and"
        " the engine name the same day",
        trigger=_job(
            HOLCAL_BLOCK,
            date_conditions="1",
            run_calendar=f'"{HOLCAL_NAME}"',
            start_times='"08:00"',
        ),
        quiet=_job(date_conditions="1", days_of_week="all", start_times='"08:00"'),
    ),
    _row(
        surface="runtime",
        member="preflight-no-start-skips-probe",
        klass=SUPPORTED,
        cite="DL-213, runner_preflight.preflight",
        effect="a preflight called with no run anchor takes now as the anchor, so the"
        " exhaustion and dormancy probes run on every call; a caller that wants a fixed"
        " answer passes a fixed anchor. The probe is day-granular: a last eligible day"
        " equal to the anchor's day passes even when its start times have passed",
        trigger=_job(HOLCAL_BLOCK, date_conditions="1", run_calendar=f'"{HOLCAL_NAME}"'),
        quiet=_job(date_conditions="1", days_of_week="all"),
    ),
    _row(
        surface="runtime",
        member="calendar-nesting-cap",
        klass=REFUSED,
        cite="autocal._parse_rule",
        bound="100 levels",
        effect="a rule nested deeper than 100 levels is refused; the bound is the"
        " parser's own recursion budget, not a documented vendor limit",
        trigger=_cal("condition: " + _nested("DAILY", 101)),
        quiet=_cal("condition: " + _nested("DAILY", 3)),
    ),
    _row(
        surface="runtime",
        member="unsized-capacity",
        klass=REFUSED,
        cite="runner_preflight._resource_preflight, DL-50",
        effect="preflight refuses a run over an unsized resource; a direct oracle caller"
        " bypasses that guard and runs unthrottled, and there the malformed values go"
        " quiet -- a malformed job_load reads as zero demand, a malformed priority as"
        " unset, and a malformed amount omits the bucket altogether",
        trigger=_job("insert_resource: R0\nres_type: R", resources="(R0, QUANTITY=1)"),
        quiet=_job(RESOURCE_BLOCK, resources="(R0, QUANTITY=1)"),
    ),
    _row(
        surface="runtime",
        member="calendar-row-seconds-truncation",
        klass=SUPPORTED,
        cite="autocal.standard_rows, DL-60",
        effect="a date row's seconds are dropped: ticks are minute-grained",
        trigger=_stmt("calendar: SC0\n01/01/2026 08:30:45"),
        quiet=_stmt("calendar: SC0\n01/01/2026 08:30"),
    ),
    _row(
        surface="runtime",
        member="sla-offset-broadcast",
        klass=PROVISIONAL,
        cite="SEM-34, ir._Lowerer._sla_attr, oracle.Oracle._sla_offset",
        effect="one relative offset broadcasts to every start slot, which SEM-34 marks"
        " open -- the strict count rule and the vendor's own example disagree; no label"
        " was opened for it",
        trigger=_job(date_conditions="1", start_times='"08:00,12:00"', must_start_times='"+30"'),
        quiet=_job(date_conditions="1", start_times='"08:00,12:00"', must_start_times='"+30,+60"'),
    ),
)


ROWS: tuple[Row, ...] = (
    STATEMENT_ROWS
    + JOB_ATTR_ROWS
    + MACHINE_ATTR_ROWS
    + RESOURCE_ATTR_ROWS
    + XINST_ATTR_ROWS
    + GLOBAL_ATTR_ROWS
    + CALENDAR_ATTR_ROWS
    + VALUE_ROWS
    + COND_ROWS
    + CALENDAR_ROWS
    + SCENARIO_ROWS
    + PROFILE_ROWS
    + ADAPTER_ROWS
    + EVENT_SOURCE_ROWS
    + TRACE_MARKER_ROWS
    + PREFLIGHT_CODE_ROWS
    + DEMAND_ROWS
    + MACHINE_VERDICT_ROWS
    + WRAPPER_OUTCOME_ROWS
    + CAL_FORM_ROWS
    + LITERAL_ALT_ROWS
    + RUNTIME_ROWS
)
