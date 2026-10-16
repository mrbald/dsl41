"""What `minify` may emit: the classification table and its value predicates.

This is the policy half of the estate minifier (`minify.py` is the mechanism half). It answers
"may this byte leave the estate" from data, a table and a predicate per key, as a pure function
of one attribute.

Every attribute key and statement subcommand resolves to exactly one class:

  KEEP     the value goes out verbatim after `validate_keep` proves it lies in the closed space
           its key claims (a vendor enum, number, clock time, day token, IANA zone, date,
           SEM-37 calendar keyword). Lowering polices only about half these keys, so the check
           is the class.
  RENAME   the value is an identifier mapped into a synthetic namespace; the rule name selects
           the rewrite.
  REPLACE  free text whose presence is semantic, swapped for a fixed inert constant. `command`
           is the only member: lowering refuses a CMD job without one.
  DROP     not emitted: the IR carries it opaquely and the value is free text or a site name.

A key in none of the four is a refusal: `classify` answers None and the caller names it. An
unknown attribute may neither leak (KEEP by default) nor vanish (DROP by default); DL-07 takes
that stance at lowering and this table one layer earlier.

The table follows what the IR models: `ANNOTATION_ATTRS`, `PASSTHROUGH_ALLOWED`, `TIME_CLUSTER`,
`EXEC_BASE_ATTRS` and the box-inert cluster in `ir.py`, plus the keys `_Lowerer` pops by name
per statement kind.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from enum import Enum
from typing import get_args

from dsl41.ir import InitialStatus, unquote_jil_value

#: What `command` becomes: a CMD job needs one, and its text is free-form client script.
INERT_COMMAND = "sleep 0"

#: SEM-24 definition-time statuses, the only ones lowering models.
_DEFINITION_STATUSES = frozenset(get_args(InitialStatus))


class Klass(Enum):
    """The four classes every attribute resolves to."""

    KEEP = "keep"
    RENAME = "rename"
    REPLACE = "replace"
    DROP = "drop"


# ------------------------------------------------------------ the table

#: Values whose space is a vendor enum, number, date, clock time or day token, so none can name
#: an estate: job_type, SEM-09 code sets, the SEM-30 time cluster, SEM-34 SLAs, SEM-24 initial
#: status, and numeric timing hints.
_KEEP: frozenset[str] = frozenset(
    {
        # identity and shape of the job
        "job_type",
        "box_terminator",
        "job_terminator",
        "status",
        "auto_hold",
        "auto_delete",
        # SEM-30 time cluster minus the two calendar references (those RENAME)
        "date_conditions",
        "days_of_week",
        "start_times",
        "start_mins",
        "run_window",
        "timezone",
        "must_start_times",
        "must_complete_times",
        # SEM-09 / DL-33 exit-code policy and retry
        "max_exit_success",
        "success_codes",
        "fail_codes",
        "n_retrys",
        # numeric timing hints: not identifying, load-bearing for durations
        "avg_runtime",
        "term_run_time",
        "max_run_alarm",
        "min_run_alarm",
        "heartbeat_interval",
        # file-watcher tuning (the watched path itself renames)
        "watch_interval",
        "watch_file_min_size",
        # placement/exec numbers carried in passthrough
        "job_load",
        "priority",
        "ulimit",
        "elevated",
        "interactive",
        # insert_machine / insert_resource / insert_xinst typed lanes
        "type",
        "factor",
        "max_load",
        "res_type",
        "amount",
        "xtype",
        "xport",
        # autocal calendars and cycles (DL-36, SEM-36..39): day tokens, flags,
        # dates and vendor condition keywords
        "workday",
        "non_workday",
        "holiday",
        "adjust",
        "prefixed",
        "start_date",
        "end_date",
    }
)

#: Carried opaquely, with a free-text or site-chosen value: the ANNOTATION_ATTRS observability
#: family, exec-cluster paths, and passthrough entries in a site's own value space.
_DROP: frozenset[str] = frozenset(
    {
        "description",
        "owner",
        "permission",
        "group",
        "application",
        "profile",
        "envvars",
        "std_in_file",
        "std_out_file",
        "std_err_file",
        "chk_files",
        "job_class",
        "machine_method",
        "alarm_if_fail",
        "alarm_if_terminated",
        "send_notification",
        "notification_msg",
        "notification_emailaddress",
        "notification_alarm_types",
        "notification_template",
        "notification_emailaddress_on_alarm",
        "notification_emailaddress_on_failure",
        "notification_emailaddress_on_success",
        "notification_emailaddress_on_terminated",
        # insert_xinst connection plumbing (DL-28)
        "xmanager",
        "xcrypt_type",
        "xkey_to_manager",
    }
)

#: key -> rewrite rule. Each rule name is a branch of `_rewrite_value`.
_RENAME: dict[str, str] = {
    "box_name": "job",
    "machine": "machine",
    "node_name": "machine",
    "xmachine": "machine",
    "run_calendar": "calendar",
    "exclude_calendar": "calendar",
    "holcal": "calendar",
    "cyccal": "calendar",
    "watch_file": "watch",
    "value": "gvalue",
    "resources": "resources",
    "condition": "cond",
    "box_success": "cond",
    "box_failure": "cond",
}

#: subcommand -> the namespace its subject belongs to. An absent subcommand is refused:
#: `rename_job`, the blob/glob/monbro/job_type/connectionprofile classes and new ones carry
#: subjects this module cannot map.
SUBJECT_NAMESPACE: dict[str, str] = {
    "insert_job": "job",
    "update_job": "job",
    "delete_job": "job",
    "override_job": "job",
    "delete_box": "job",
    "insert_machine": "machine",
    "update_machine": "machine",
    "delete_machine": "machine",
    "insert_global": "global",
    "delete_global": "global",
    "insert_resource": "resource",
    "update_resource": "resource",
    "delete_resource": "resource",
    "insert_xinst": "instance",
    "update_xinst": "instance",
    "delete_xinst": "instance",
    "calendar": "calendar",
    "extended_calendar": "calendar",
    "ext_calendar": "calendar",
    "cycle": "calendar",
}

_MACHINE_SUBCOMMANDS = frozenset({"insert_machine", "update_machine", "delete_machine"})
_CALENDAR_SUBCOMMANDS = frozenset({"calendar", "extended_calendar", "ext_calendar", "cycle"})
JOB_SUBCOMMANDS = frozenset({"insert_job", "update_job", "delete_job", "override_job"})
#: The BOX spellings `ir._JOB_TYPE_MAP` accepts, re-stated not imported (DL-74).
BOX_JOB_TYPES = frozenset({"b", "box"})
_JOB_TYPES = frozenset({"c", "cmd", "f", "fw"}) | BOX_JOB_TYPES


def classify(subcommand: str, key: str) -> tuple[Klass, str] | None:
    """The class of one attribute in one statement kind, or None to refuse.

    Only `condition:` is context-dependent: inside an autocal calendar it is a SEM-36/37
    date-condition expression naming no job, so it keeps, validated against the SEM-37
    inventory (`validate_keep`). `machine:` is not: a pool member line (DL-49) and a job's exec
    placement are both comma lists that L017 splits alike.
    """
    sub, k = subcommand.lower(), key.lower()
    if sub in _CALENDAR_SUBCOMMANDS and k == "condition":
        return (Klass.KEEP, "")
    if k in _RENAME:
        return (Klass.RENAME, _RENAME[k])
    if k == "command":
        return (Klass.REPLACE, INERT_COMMAND)
    if k in _KEEP:
        return (Klass.KEEP, "")
    if k in _DROP:
        return (Klass.DROP, "")
    return None


def class_counts() -> dict[str, int]:
    """Table size per class."""
    return {
        Klass.KEEP.value: len(_KEEP),
        Klass.RENAME.value: len(_RENAME),
        Klass.REPLACE.value: 1,
        Klass.DROP.value: len(_DROP),
    }


def _v_calendar_condition(value: str) -> bool:
    """SEM-36/37 date-condition expression, checked by `compile_calendar`, the one owner of the
    keyword grammar; a site label (`ACMEBANK`) refuses.

    The stand-in calendar always declares stub holcal and cyccal, so only the expression's
    vocabulary is validated; autocal refuses an unresolved cycle-scoped token (`CWRK#L`) at
    consumption.
    """
    from dsl41.autocal import compile_calendar
    from dsl41.ir import CalendarIR, CatalogIR, CycleIR

    catalog = CatalogIR(
        calendars={"<holcal>": CalendarIR(name="<holcal>", kind="standard")},
        cycles={"<cyccal>": CycleIR(name="<cyccal>", periods=[("01/01/2026", "12/31/2026")])},
    )
    cal = CalendarIR(
        name="<minify>",
        kind="extended",
        attrs={"holcal": "<holcal>", "cyccal": "<cyccal>"},
        conditions=[value.strip()],
    )
    try:
        # default switches: no switch changes what the parser accepts (DL-259)
        compile_calendar(cal, catalog)
    except Exception:  # noqa: BLE001
        # broad on purpose: a predicate answers yes or no, and an escape would turn a refusal
        # into a traceback on the path that must fail closed
        return False
    return True


# --------------------------------------------------- the KEEP value predicates

#: KEEP means checked to be in a closed space, never assumed. Lowering carries about half of
#: these keys opaquely, so a client string in `timezone` or `job_load` lowers clean. Every KEEP
#: key has a predicate, and a value that does not match is a refusal.
_INT_RE = re.compile(r"[+-]?\d+")
_NUM_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
#: jil-statement-syntax rule 2 (the vendor's rule 6): a colon in an unquoted value may be
#: escaped (`10\:00`). dsl41 applies it to every `hh:mm` lane, so the escaped spelling is in
#: the closed space too (DL-251).
_TIME_RE = re.compile(r"\d{1,2}\\?:\d{2}")
_REL_RE = re.compile(r"\+\d+")
_CODE_RE = re.compile(r"\d+(?:-\d+)?")
_ONE_CHAR_RE = re.compile(r"[A-Za-z0-9]")
_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")
#: `ir._TRUTHY | ir._FALSY`, re-stated not imported (DL-74) and pinned to its source by a test
#: (DL-75).
_BOOLS = frozenset({"0", "1", "y", "n", "yes", "no", "true", "false"})
#: `ir._DAY_TOKENS | set(ir._DAY_FULL)`, re-stated and pinned like `_BOOLS`.
_DAY_TOKENS = frozenset(
    {
        "su",
        "mo",
        "tu",
        "we",
        "th",
        "fr",
        "sa",
        "all",
        "sunday",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
    }
)
#: `non_workday`/`holiday` carry an action code (SEM-38) beside the day tokens.
_DAY_OR_ACTION = _DAY_TOKENS | frozenset("osnwp")


def _items(value: str) -> list[str]:
    """A JIL comma list, unquoted and split the way lowering splits it."""
    return [t for t in (part.strip() for part in unquote_jil_value(value).split(",")) if t]


def _all_match(pattern: re.Pattern[str], value: str) -> bool:
    items = _items(value)
    return bool(items) and all(pattern.fullmatch(item) for item in items)


def _v_int(value: str) -> bool:
    return _INT_RE.fullmatch(unquote_jil_value(value)) is not None


def _v_num(value: str) -> bool:
    return _NUM_RE.fullmatch(unquote_jil_value(value)) is not None


def _v_bool(value: str) -> bool:
    return unquote_jil_value(value).lower() in _BOOLS


def _v_one_char(value: str) -> bool:
    """`type`/`res_type`/`xtype`: every documented value is one character."""
    return _ONE_CHAR_RE.fullmatch(unquote_jil_value(value)) is not None


def _v_job_type(value: str) -> bool:
    return unquote_jil_value(value).lower() in _JOB_TYPES


def _v_status(value: str) -> bool:
    return unquote_jil_value(value).upper() in _DEFINITION_STATUSES


def _v_days(value: str) -> bool:
    return all(item.lower() in _DAY_TOKENS for item in _items(value)) and bool(_items(value))


def _v_day_or_action(value: str) -> bool:
    return all(item.lower() in _DAY_OR_ACTION for item in _items(value)) and bool(_items(value))


def _v_times(value: str) -> bool:
    return _all_match(_TIME_RE, value)


def _v_sla(value: str) -> bool:
    items = _items(value)
    return bool(items) and all(
        _TIME_RE.fullmatch(item) or _REL_RE.fullmatch(item) for item in items
    )


def _v_ints(value: str) -> bool:
    return _all_match(_INT_RE, value)


def _v_codes(value: str) -> bool:
    return _all_match(_CODE_RE, value)


def _v_date(value: str) -> bool:
    return _DATE_RE.fullmatch(unquote_jil_value(value)) is not None


def _v_window(value: str) -> bool:
    parts = unquote_jil_value(value).split("-")
    return len(parts) == 2 and all(_TIME_RE.fullmatch(p.strip()) for p in parts)


#: The only non-IANA timezone spellings called closed vocabulary: fixed, published
#: abbreviations that cannot name an estate. Extend only by a deliberate edit.
TZ_ABBREVIATIONS = frozenset(
    {
        "UTC",
        "GMT",
        "Z",
        "EST",
        "EDT",
        "CST",
        "CDT",
        "MST",
        "MDT",
        "PST",
        "PDT",
        "BST",
        "CET",
        "CEST",
        "EET",
        "EEST",
        "WET",
        "WEST",
        "IST",
        "JST",
        "AEST",
        "AEDT",
        "NZST",
        "NZDT",
    }
)
#: POSIX fixed-offset shape (SEM-35): an abbreviation, then the offset.
#: Not `timezones._POSIX_FIXED`, whose letter run is unbounded and would admit
#: `ACMEBANKLONDONDESK5`; here the letters must be one of TZ_ABBREVIATIONS.
_POSIX_TZ_RE = re.compile(r"([A-Za-z]{1,4})([+-]?\d{1,2})(?::\d{2}(?::\d{2})?)?")


@functools.lru_cache(maxsize=1)
def _iana_keys() -> frozenset[str]:
    """The zoneinfo database, read once; `available_timezones()` walks tzdata on every call."""
    from zoneinfo import available_timezones

    return frozenset(available_timezones())


def _v_timezone(value: str) -> bool:
    """SEM-35 zone predicate. It enumerates and never delegates to
    `timezones.resolve_timezone`, whose permissive POSIX fallback would pass
    `ACMEBANKLONDONDESK5` as closed vocabulary.

    Accepted: exactly a canonical IANA zone key, or a POSIX fixed offset whose abbreviation is
    in `TZ_ABBREVIATIONS`. Refused: a bare city (`Zurich`, a possible site label), an alias
    spelling, and any other POSIX-shaped string. The remedy for a refused zone is
    `--scrub-timezones`.
    """
    token = unquote_jil_value(value).strip()
    if not token:
        return False
    if token in _iana_keys():
        return True
    match = _POSIX_TZ_RE.fullmatch(token)
    return match is not None and match.group(1).upper() in TZ_ABBREVIATIONS


#: key -> predicate. Every KEEP key appears; `_KEEP == _KEEP_SHAPES.keys()` is checked.
_KEEP_SHAPES: dict[str, Callable[[str], bool]] = {
    "job_type": _v_job_type,
    "status": _v_status,
    "box_terminator": _v_bool,
    "job_terminator": _v_bool,
    "auto_hold": _v_bool,
    "auto_delete": _v_bool,
    "date_conditions": _v_bool,
    "elevated": _v_bool,
    "interactive": _v_bool,
    "prefixed": _v_bool,
    "days_of_week": _v_days,
    "workday": _v_days,
    "non_workday": _v_day_or_action,
    "holiday": _v_day_or_action,
    "start_times": _v_times,
    "must_start_times": _v_sla,
    "must_complete_times": _v_sla,
    "start_mins": _v_ints,
    "run_window": _v_window,
    "timezone": _v_timezone,
    "success_codes": _v_codes,
    "fail_codes": _v_codes,
    "max_exit_success": _v_int,
    "n_retrys": _v_int,
    "avg_runtime": _v_int,
    "term_run_time": _v_int,
    "max_run_alarm": _v_int,
    "min_run_alarm": _v_int,
    "heartbeat_interval": _v_int,
    "watch_interval": _v_int,
    "watch_file_min_size": _v_int,
    "job_load": _v_int,
    "priority": _v_int,
    "ulimit": _v_int,
    "amount": _v_int,
    "xport": _v_int,
    "adjust": _v_int,
    "max_load": _v_int,
    "factor": _v_num,
    "type": _v_one_char,
    "res_type": _v_one_char,
    "xtype": _v_one_char,
    "start_date": _v_date,
    "end_date": _v_date,
    "condition": _v_calendar_condition,
}


def validate_keep(key: str, value: str) -> bool:
    """Is this KEEP value inside the closed space its key claims?"""
    predicate = _KEEP_SHAPES.get(key.lower())
    return predicate is not None and predicate(value)


def validate_date_rows(rows: list[str]) -> str | None:
    """None if rule-11 calendar date rows are dates, else why not.

    `ast_jil` carries any non-attribute line under `calendar:` verbatim, so an export's label
    column would ship as is. `autocal.standard_rows` owns the row grammar (DL-36, Q9).
    """
    if not rows:
        return None
    from dsl41.autocal import CalendarRuleError, standard_rows
    from dsl41.ir import CalendarIR

    try:
        standard_rows(CalendarIR(name="<minify>", kind="standard", dates=list(rows)))
    except CalendarRuleError as exc:
        return str(exc)
    return None


#: Words the minified output carries because JIL and dsl41 say so: the grammar keywords of the
#: value lanes a RENAME rewrite preserves, plus the inert command. Attribute keys and
#: subcommand names join them in `_vocabulary`. Statuses and day tokens derive from the sets
#: above (DL-75); the short day spellings fall under the leak guard's 4-character floor.
_VALUE_KEYWORDS = (
    frozenset(
        {
            "quantity",
            "free",
            "sleep",
            "true",
            "false",
            # SEM-02 condition keywords
            "success",
            "failure",
            "done",
            "terminated",
            "notrunning",
            "exitcode",
            "value",
        }
    )
    | frozenset(status.lower() for status in _DEFINITION_STATUSES)  # SEM-24
    | _DAY_TOKENS
)


def vocabulary_words() -> set[str]:
    """Every word the output may carry that the estate did not write: the classified keys, the
    subcommand inventory and the preserved grammar keywords. The leak guard subtracts them."""
    from dsl41.ast_jil import SUBCOMMANDS

    keys = set(_KEEP) | set(_DROP) | set(_RENAME) | {"command"}
    return keys | set(SUBCOMMANDS) | set(SUBJECT_NAMESPACE) | set(_VALUE_KEYWORDS)
