"""The simulation coverage register's rows, as literal data (DL-209).

Every row is a plain mapping validated into `simulation_register.Behaviour`
at import. The members are LITERAL here on purpose: deriving them from the
inventories would make the register agree with the code by construction and
catch nothing. The test derives the domains instead and fails on a member
with no row -- and on a row whose member the code no longer has.

Fixture strings are interpreted by `tests/test_simulation_register.py`
according to the surface's fixture kind (jil, cond, scenario, profile,
outcome). A `runtime` row says its kind in a leading `kind: <name>` line,
because that surface is free and its rows pick their own.

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
#: a job with no condition string at all: nothing parses, so nothing is seen
BASE_COND = ""
BASE_SCENARIO = _scn(BASE_JIL)
BASE_PROFILE = "{}"
BASE_OUTCOME = "int"

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

_SUPPORTED_STATEMENTS: dict[str, tuple[str, str, str]] = {
    # member: (cite, effect, the statement text the trigger appends)
    "insert_job": (
        "DL-29",
        "a job definition is lowered whole: linkage, semantics, schedule and exec spec",
        "",
    ),
    "insert_machine": (
        "DL-49",
        "a machine definition is lowered to its type, node_name and pool members",
        "",
    ),
    "insert_global": (
        "SEM-08",
        "a global variable's declared value seeds the oracle's global store",
        "insert_global: G0\nvalue: 0",
    ),
    "insert_resource": (
        "DL-50",
        "a resource definition sizes one capacity bucket",
        RESOURCE_BLOCK,
    ),
    "insert_xinst": (
        "SEM-07",
        "an external-instance definition is carried; cross-instance atoms read it by name",
        "insert_xinst: X0\nxtype: a",
    ),
    "calendar": (
        "SEM-36, DL-36",
        "a standard calendar's date rows become the day set holcal and run_calendar read",
        "calendar: SC0\n01/01/2026",
    ),
    "extended_calendar": (
        "SEM-36, DL-36",
        "an extended calendar's rules compile to a day generator",
        "extended_calendar: EC0\ncondition: DAILY",
    ),
    "ext_calendar": (
        "SEM-36, DL-60",
        "the Manage Calendars spelling of extended_calendar, accepted as input leniency",
        "ext_calendar: EC1\ncondition: DAILY",
    ),
    "cycle": (
        "SEM-39",
        "a cycle's start_date/end_date pairs become the periods cycle-scoped tokens count in",
        "cycle: CY0\nstart_date: 01/01/2026\nend_date: 03/31/2026",
    ),
}

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

STATEMENT_ROWS: tuple[Row, ...] = tuple(
    _row(
        surface="statement",
        member=member,
        klass=SUPPORTED,
        cite=cite,
        effect=effect,
        trigger=_stmt(extra) if extra else BASE_JIL,
        quiet=GLOBAL_JIL if member in ("insert_job", "insert_machine") else None,
    )
    for member, (cite, effect, extra) in _SUPPORTED_STATEMENTS.items()
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
        {"run_calendar": '"HOLS"'},
    ),
    "exclude_calendar": (
        "SEM-30, DL-56",
        "the named calendar whose days are subtracted from the schedule's day set",
        {"exclude_calendar": '"HOLS"'},
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
        "an alarm only: a missed start raises MUST_START_ALARM and changes no status",
        {"start_times": '"08:00"', "must_start_times": '"+30"'},
    ),
    "must_complete_times": (
        "SEM-34",
        "an alarm only: a missed completion raises MUST_COMPLETE_ALARM and changes no status",
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
        "overrides a box's success verdict; evaluated only when the box is done",
        {"job_type": "b", "command": None, "machine": None, "box_success": "s(J1)"},
    ),
    "box_failure": (
        SUPPORTED,
        "SEM-12",
        "overrides a box's failure verdict; evaluated only when the box is done",
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
            marker=True,
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
            marker=True,
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
            trigger=_job(date_conditions="1", **extra),
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
            marker=True,
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
            marker=True,
            effect="a start time inside a DST fold or gap resolves by the pinned"
            " interpretation, not by a vendor-verified rule",
            trigger=_job(date_conditions="1", timezone="Europe/Berlin", start_times='"02:30"'),
            quiet=_job(date_conditions="1", timezone="UTC", start_times='"02:30"'),
        ),
        _row(
            surface="job_attr",
            member="exclude_calendar",
            facet="two-year-probe",
            klass=SUPPORTED,
            cite="DL-56, runner_scheduler",
            bound="731 days",
            effect="an exclusion that leaves no eligible day inside 731 days reports the"
            " schedule as exhausted; absence is proven within that bound only",
            trigger=_job(date_conditions="1", days_of_week="all", exclude_calendar='"HOLS"'),
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
            marker=True,
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
            member="condition",
            facet="queued-no-recheck",
            klass=PROVISIONAL,
            cite="DL-50, oracle.Oracle._readmit",
            label="Qr6",
            marker=True,
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
            marker=False,
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
            cite="dossier ss6",
            effect="the shell command the CMD adapter spawns, passed to /bin/sh verbatim",
            trigger=BASE_JIL,
            quiet=_job(**_FW_JOB),
        ),
        _row(
            surface="job_attr",
            member="watch_file",
            klass=SUPPORTED,
            cite="dossier ss6",
            effect="the path an FW job polls; the job completes when the file arrives",
            trigger=_job(**_FW_JOB),
        ),
        _row(
            surface="job_attr",
            member="watch_interval",
            klass=SUPPORTED,
            cite="dossier ss6",
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
            marker=True,
            effect="an FW job with no watch_interval polls at the profile's default interval",
            trigger=_job(**_FW_JOB),
            quiet=_job(watch_interval="30", **_FW_JOB),
        ),
        _row(
            surface="job_attr",
            member="watch_file_min_size",
            klass=SUPPORTED,
            cite="dossier ss6",
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
            marker=True,
            effect="the size is read once per poll; a file still growing is not waited out",
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
        marker=True,
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
        "a uniform blind day shift applied to every surviving day, -9..+9",
        "adjust: 1",
    ),
}

CALENDAR_ATTR_ROWS: tuple[Row, ...] = tuple(
    _row(
        surface="calendar_attr",
        member=member,
        klass=SUPPORTED,
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
        marker=True,
        protocol="Q8b",
        effect="disposition replaces first, then the blind adjust shifts every survivor",
        trigger=_cal("condition: DAILY", "adjust: 1", "non_workday: N"),
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
            marker=True,
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
            marker=False,
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

_COND_TERMINALS: dict[str, tuple[str, str]] = {
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
            effect=f"the grammar lexes {member} and the transformer gives it its SEM meaning",
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

_CAL_FAMILIES: dict[str, str] = {
    # family name: a token of that family
    "workd": "WORKD#1",
    "weekd": "WEEKD#1",
    "wekr": "WEKRMON#1",
    "week_parity": "WEEK#E",
    "week": "WEEK#1",
    "mnthd": "MNTHD#1",
    "month_ordinal": "JAN#1",
    "day_ordinal": "MON#1",
    "cycl": "CYCL#1",
    "cycp": "CYCP#1",
    "cweek_parity": "CWEEK#E",
    "cweek": "CWEEK#2",
    "cwrk": "CWRK#1",
    "cddd": "CMON#1",
}

#: The families whose tokens only mean anything inside a cycle's periods;
#: their calendars need a `cyccal` or `compile_calendar` refuses them.
_CYCLE_SCOPED_FAMILIES = frozenset({"cycl", "cycp", "cweek_parity", "cweek", "cwrk", "cddd"})

_DEFECTIVE_FAMILIES: dict[str, str] = {"workdx": "WORKDX1", "cwek": "CWEK#1"}

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
            effect=f"the {member} ordinal family generates its documented day set",
            trigger=_cal(f"condition: {token}", cyccal=member in _CYCLE_SCOPED_FAMILIES),
        )
        for member, token in _CAL_FAMILIES.items()
    )
    + tuple(
        _row(
            surface="cal_family",
            member=member,
            klass=REFUSED,
            cite="autocal._parse_token, SEM-37",
            effect=f"the {member} family is doc-defective: the vendor's own text contradicts"
            " itself, so the token is refused rather than guessed",
            trigger=_cal(f"condition: {token}"),
        )
        for member, token in _DEFECTIVE_FAMILIES.items()
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
            marker=True,
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
            marker=True,
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
            marker=True,
            protocol=_CAL_PROTOCOL,
            effect="& and | evaluate flat left-to-right, with no precedence between them",
            trigger=_cal("condition: MON | JAN & TUE"),
            quiet=_cal("condition: MON"),
        ),
        _row(
            surface="cal_operator",
            member="and",
            facet="word-synonym",
            klass=PROVISIONAL,
            cite="SEM-37, DL-59",
            label="Q8d",
            marker=True,
            protocol=_CAL_PROTOCOL,
            effect="AND is pinned as an exact synonym of &",
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
            marker=True,
            protocol=_CAL_PROTOCOL,
            effect="OR is pinned as an exact synonym of |",
            trigger=_cal("condition: MON OR TUE"),
            quiet=_cal("condition: MON | TUE"),
        ),
    )
    + _rows(
        "cal_action",
        ("o", "s"),
        klass=SUPPORTED,
        cite="SEM-38",
        effect="action {member} filters the category without moving any date",
        trigger=lambda m: _cal("condition: DAILY", f"non_workday: {m.upper()}"),
    )
    + _rows(
        "cal_action",
        ("n", "w", "p"),
        klass=SUPPORTED,
        cite="SEM-38",
        effect="action {member} replaces an excluded date with a walked target day",
        trigger=lambda m: _cal("condition: DAILY", f"non_workday: {m.upper()}"),
    )
    + tuple(
        _row(
            surface="cal_action",
            member=member,
            facet="target-recheck",
            klass=PROVISIONAL,
            cite="SEM-38, DL-59",
            label="Q8c",
            marker=True,
            protocol=_CAL_PROTOCOL,
            effect="the replacement target is final: the date-conditions are not re-checked"
            " and a replacement never re-enters the other category",
            trigger=_cal("condition: DAILY", f"non_workday: {member.upper()}", holcal=True),
            quiet=_cal("condition: DAILY", f"non_workday: {member.upper()}"),
        )
        for member in ("n", "w", "p")
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
            marker=True,
            protocol="Q3d",
            effect="a pre-existing arm survives the ice round trip untouched",
            trigger=_scn(BASE_JIL, "0 ON_ICE job=J0", "1 OFF_ICE job=J0"),
            quiet=_scn(BASE_JIL, "0 ON_HOLD job=J0", "1 OFF_HOLD job=J0"),
        ),
        _row(
            surface="event",
            member="ON_ICE",
            facet="queued",
            klass=PROVISIONAL,
            cite="DL-50, oracle.Oracle._handle_oob",
            label="Qr5",
            marker=False,
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
            marker=False,
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

_PROFILE_ALTS: dict[str, str] = {
    "machine_policy=strict": "a job whose machine does not resolve local is refused",
    "machine_policy=local-eligible": "an unresolvable machine is treated as eligible here",
    "execution_mode=tethered": "the engine owns the child processes; there is no supervisor",
    "execution_mode=detached": "a supervisor owns the child processes across engine restarts",
}

PROFILE_ROWS: tuple[Row, ...] = tuple(
    _row(
        surface="profile_field",
        member=member,
        klass=SUPPORTED,
        cite=cite,
        effect=effect,
        trigger=override,
    )
    for member, (cite, effect, override) in _PROFILE_FIELDS.items()
) + tuple(
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


# ------------------------------------------------------------------ adapters

_CRASH_CAUSE = "dispatch lost to engine crash (run directory missing)"

ADAPTER_ROWS: tuple[Row, ...] = (
    _row(
        surface="adapter_outcome",
        member="int",
        klass=SUPPORTED,
        cite="dossier ss6, SEM-09",
        effect="a raw exit code; the SUCCESS/FAILURE verdict over it stays oracle-side",
        trigger="int",
        quiet="Terminated",
    ),
    _row(
        surface="adapter_outcome",
        member="Terminated",
        klass=SUPPORTED,
        cite="dossier ss6, DL-41a",
        effect="an OBSERVED kill; the engine injects STATUS TERMINATED for it",
        trigger="Terminated",
    ),
    _row(
        surface="adapter_outcome",
        member="Failed",
        klass=SUPPORTED,
        cite="dossier ss6",
        effect="a completion with no raw exit code; the engine injects STATUS FAILURE"
        " with the cause",
        trigger="Failed",
    ),
    _row(
        surface="adapter_outcome",
        member="Failed=exit_status_unobservable",
        klass=PROVISIONAL,
        cite="runner_adapters, runner-design ss15",
        label="E7",
        marker=True,
        effect="a wrapper that exited without a status record fails the run rather than"
        " guessing an exit code",
        trigger="Failed=exit_status_unobservable",
    ),
    _row(
        surface="adapter_outcome",
        member=f"Failed={_CRASH_CAUSE}",
        klass=SUPPORTED,
        cite="runner_adapters.resolve_spool, DL-118",
        effect="a dispatch whose run directory is gone provably never reached the host,"
        " so it fails rather than being retried blind",
        trigger=f"Failed={_CRASH_CAUSE}",
    ),
    _row(
        surface="adapter_outcome",
        member="Terminated",
        facet="external-signal",
        klass=PROVISIONAL,
        cite="runner_adapters",
        label="E8",
        marker=True,
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


# ------------------------------------------------------------------ runtime

RUNTIME_ROWS: tuple[Row, ...] = (
    _row(
        surface="runtime",
        member="member-arm-scope",
        klass=PROVISIONAL,
        cite="oracle.Oracle._after_transition",
        label="Q3c",
        marker=True,
        protocol="Q3c",
        effect="a box member's latched tick is scoped to the box run it was latched in",
        trigger="kind: jil\n" + _job(BOX_BLOCK, box_name="BOX0", condition="s(BOX0)"),
        quiet="kind: jil\n" + BASE_JIL,
    ),
    _row(
        surface="runtime",
        member="missed-tick-skip",
        klass=PROVISIONAL,
        cite="runner_scheduler.Scheduler.pop_due, runner_startup",
        label="E9",
        marker=True,
        effect="a tick whose instant passed while the engine was down is journaled and"
        " dropped, never fired late",
        trigger="kind: jil\n" + SCHEDULED_JOB,
        quiet="kind: jil\n" + BASE_JIL,
    ),
    _row(
        surface="runtime",
        member="exclusion-only-compound",
        klass=PROVISIONAL,
        cite="autocal.compile_calendar, DL-59",
        label="Q8d",
        marker=True,
        protocol=_CAL_PROTOCOL,
        effect="a compound rule with no inclusive leaf is evaluated literally as an include,"
        " which makes it near-universal",
        trigger="kind: jil\n" + _cal("condition: XTUE|XWED"),
        quiet="kind: jil\n" + _cal("condition: DAILY"),
    ),
    _row(
        surface="runtime",
        member="scan-horizon",
        klass=SUPPORTED,
        cite="autocal._SCAN_YEARS, runner_scheduler._EXTENDED_SCAN_DAYS",
        bound="60 years",
        effect="a calendar that generates nothing within 60 years reads as exhausted;"
        " dormancy is proven within that bound only",
        trigger="kind: jil\n" + _cal("condition: FEB#29 & MON"),
        quiet="kind: jil\n" + _cal("condition: DAILY"),
    ),
    _row(
        surface="runtime",
        member="walk-cap",
        klass=REFUSED,
        cite="autocal.CompiledCalendar._walk",
        bound="366 days",
        effect="a W/P replacement that finds no valid day within 366 days is degenerate"
        " and refuses the calendar",
        trigger="kind: jil\n"
        + _estate(
            MACHINE_BLOCK,
            JOB_BLOCK,
            "extended_calendar: EC0\ncondition: DAILY\nworkday: x......\n"
            f"holiday: W\nholcal: {HOLCAL_NAME}",
            STARVED_HOLCAL_BLOCK,
        ),
        quiet="kind: jil\n"
        + _estate(
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
        trigger="kind: jil\n" + _cal("condition: DAILY", "non_workday: W", "workday: ......."),
        quiet="kind: jil\n" + _cal("condition: DAILY", "non_workday: W", "workday: x......"),
    ),
    _row(
        surface="runtime",
        member="unsized-capacity",
        klass=REFUSED,
        cite="runner_preflight._resource_preflight, DL-50",
        effect="preflight refuses a run over an unsized resource; a direct oracle caller"
        " bypasses that guard and runs unthrottled",
        trigger="kind: jil\n"
        + _job("insert_resource: R0\nres_type: R", resources="(R0, QUANTITY=1)"),
        quiet="kind: jil\n" + _job(RESOURCE_BLOCK, resources="(R0, QUANTITY=1)"),
    ),
    _row(
        surface="runtime",
        member="calendar-row-seconds-truncation",
        klass=SUPPORTED,
        cite="autocal.standard_rows, DL-60",
        effect="a date row's seconds are dropped: ticks are minute-grained",
        trigger="kind: jil\n" + _stmt("calendar: SC0\n01/01/2026 08:30:45"),
        quiet="kind: jil\n" + _stmt("calendar: SC0\n01/01/2026 08:30"),
    ),
    _row(
        surface="runtime",
        member="sla-offset-broadcast",
        klass=SUPPORTED,
        cite="SEM-34, ir._Lowerer._sla_attr",
        effect="one relative offset broadcasts to every start slot",
        trigger="kind: jil\n"
        + _job(date_conditions="1", start_times='"08:00,12:00"', must_start_times='"+30"'),
        quiet="kind: jil\n"
        + _job(date_conditions="1", start_times='"08:00,12:00"', must_start_times='"+30,+60"'),
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
    + RUNTIME_ROWS
)
