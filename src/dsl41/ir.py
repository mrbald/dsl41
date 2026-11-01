"""IR-F: faithful semantic layer, entity models and AST -> IR-F lowering.

Phase 3 of the DL-03 order. Normative spec: docs/ir-design.md ss3-4 (models are the API;
lowering rules) and docs/autosys-semantics.md (SEM entries cited per field). Condition models
live in conditions.py (ss3); this module holds the entity models (ss4), the lowering with the
DL-07 passthrough firewall, and deterministic serialization (ss8).

Loss policy (ir-design ss1): lowering may normalize syntax (abbreviations, quoting, list
formats) but never semantics. An unknown attribute not on the inert allow-list is a lowering
error unless `permit_unknown` is set (DL-07).

Rules, each pinned by a test:
- SEM-30: time attributes with falsy or absent `date_conditions` are dead config. They go to
  `passthrough` verbatim, not into the schedule, so L005 can see them.
- SEM-34: the must_*_times count matches start_times, except a single relative offset, which
  broadcasts to every start time. [?] The dossier says strict count-match, but its doc-derived
  fixture (sem30_schedule.jil) uses one `+3` against three start_times, TechDocs' own example.
  Pin the exact rule on a live instance.
- Subcommands: insert_job, insert_global, insert_machine, insert_xinst, insert_resource
  (DL-18), and the autocal_asc statements calendar, cycle and extended_calendar, carried
  opaquely (DL-36). update_, delete_ and override_ forms are lowering errors: merging them is
  semantics, and guessing it would be silent loss.
- SEM-24 [A] (DL-18): `status:` on insert_job lowers to Semantics.initial_status, limited to
  INACTIVE, ON_HOLD, ON_ICE and ON_NOEXEC. Run states would interact with the SEM-01 latch and
  are refused.
- DL-21: the `resources:` attribute lowers to typed JobIR.resources (name, QUANTITY, FREE per
  group, AND separator); malformed groups are errors. QUANTITY is required. An absent FREE
  stays None, because the engine default is not guessed.
- job_type is required, with no defaulting to CMD. A missing one in hand-written JIL is more
  likely an error than a default. [?] Relax if a real-estate fixture needs it (dossier ss5).
- Type-inapplicable exec attributes are lowering errors: command on BOX or FW, watch_* on CMD or
  BOX, std_in_file and envvars on FW. Machine, owner, profile, std_* and envvars on a BOX are
  inert (SEM-10) and go to passthrough verbatim.
- Duplicate attribute keys in a statement and duplicate job names in a catalog are lowering
  errors, because last-wins would be silent loss. L014 covers UC-side name collision.
- Semantic unquoting (statement-syntax rule 7) applies to the typed lane only (command and
  exec strings, box_name, calendars, timezone, global values, machine type, xtype): exactly one
  wrapping quote pair, no interior quotes. annotations and passthrough are verbatim lanes (ss4).
  MachineIR.attrs and ResourceIR.attrs are opaque (dossier ss5, DL-18); the DL-07 firewall
  guards job semantics, not machine or resource records.
"""

from __future__ import annotations

import json
import re
from collections.abc import Container, Iterable, Iterator, Sequence
from importlib import metadata
from typing import Annotated, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from dsl41.ast_jil import JilFile, JilStatement, RawAttr, SourceSpan
from dsl41.ast_jil import parse as parse_jil
from dsl41.canon import is_scalar_json, is_scalar_string
from dsl41.conditions import Cond, ConditionParseError, parse_condition, unescape_job_name

# ------------------------------------------------------------- attribute inventories

#: Observability-class attributes (dossier ss5: "no control flow") -> JobIR.annotations.
ANNOTATION_ATTRS = frozenset(
    {
        "description",
        "alarm_if_fail",
        "alarm_if_terminated",
        "min_run_alarm",
        "max_run_alarm",
        "send_notification",
        "notification_msg",
        "notification_emailaddress",
        # DL-32: MISSING_HEARTBEAT alarm and the notification-services family, no control flow
        "heartbeat_interval",
        "notification_alarm_types",
        "notification_template",
        "notification_emailaddress_on_alarm",
        "notification_emailaddress_on_failure",
        "notification_emailaddress_on_success",
        "notification_emailaddress_on_terminated",
    }
)

#: Known-inert / carried-opaquely-v1 attributes (dossier ss5) -> JobIR.passthrough.
#: auto_delete: definition lifecycle. job_load, priority, machine_method: resource and placement
#: (M34). ulimit, elevated, interactive, job_class: OS and agent exec tuning. avg_runtime:
#: statistics seed. chk_files: a pre-start disk-space gate of the Resource-Wait class (dossier
#: ss5), with no oracle semantics (DL-32).
PASSTHROUGH_ALLOWED = frozenset(
    {
        "auto_delete",
        "permission",
        "group",
        "application",
        "job_load",
        "priority",
        "machine_method",
        "job_class",
        "avg_runtime",
        "ulimit",
        "elevated",
        "interactive",
        "chk_files",
    }
)

#: SEM-30 time cluster: honored only when date_conditions is truthy.
TIME_CLUSTER = frozenset(
    {
        "days_of_week",
        "run_calendar",
        "exclude_calendar",
        "start_times",
        "start_mins",
        "run_window",
        "timezone",
        "must_start_times",
        "must_complete_times",
    }
)

_JOB_TYPE_MAP = {"c": "CMD", "cmd": "CMD", "b": "BOX", "box": "BOX", "f": "FW", "fw": "FW"}

#: SEM-24 [A]: `status:` at definition time. Only the implicit default and the
#: SEM-20/21/22 out-of-band states are modeled; run states (SUCCESS, ...) would
#: interact with the SEM-01 latch and are refused, never guessed.
InitialStatus = Literal["INACTIVE", "ON_HOLD", "ON_ICE", "ON_NOEXEC"]
_INITIAL_STATUSES: frozenset[str] = frozenset(get_args(InitialStatus))
_TRUTHY = frozenset({"1", "y", "yes", "true"})
_FALSY = frozenset({"0", "n", "no", "false"})
_DAY_TOKENS = frozenset({"su", "mo", "tu", "we", "th", "fr", "sa", "all"})
_DAY_FULL = {
    "sunday": "su",
    "monday": "mo",
    "tuesday": "tu",
    "wednesday": "we",
    "thursday": "th",
    "friday": "fr",
    "saturday": "sa",
}

#: SEM-08: `$$NAME` and `$${NAME}` are AutoSys globals; a single `$` is shell and never matches.
_VAR_RE = re.compile(r"\$\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

#: SEM-34 relative must_*_times token: a plus sign and decimal digits. `\d` would take non-ASCII
#: digits, so the class is explicit.
_REL_OFFSET_RE = re.compile(r"\+[0-9]+")

#: Lexical HH:MM shape shared by Time.parse and MustTime.parse; each type's Field bound decides
#: the range (DL-251, DL-253).
_HHMM_RE = re.compile(r"(\d{1,2}):(\d{2})")

#: SlaSpec.times holds MustTime, whose hour runs to 71 (DL-253).
IR_VERSION: Literal["0.3"] = "0.3"


def _parse_hhmm(text: str) -> tuple[int, int]:
    """Lexical HH:MM. A colon in an unquoted value may be escaped (`10\\:00`) instead of quoting
    the value (jil-statement-syntax rule 2); every `hh:mm` lane unescapes it (DL-251).

    Per-token quoting of one list item (`"10:00", "14:00"`) is not documented and is not
    unwrapped; only a whole-value quote is, upstream, by `unquote_jil_value` and `_split_list`.
    """
    m = _HHMM_RE.fullmatch(text.strip().replace("\\:", ":"))
    if m is None:
        raise ValueError(f"invalid time {text!r} (expected HH:MM)")
    return int(m.group(1)), int(m.group(2))


def _must_time_order(starts: list[Time], musts: list[MustTime]) -> str | None:
    """SEM-34 (DL-253): the vendor's two ordering rules for absolute must times, paired with
    start_times by position. Returns the refusal message, or None when both hold.

    A must time below its own start time is refused; the next day's 10:00 is written 34:00. A
    must time not earlier than the next run's start time is refused (11:10 against an 11:00
    run). The next run is the next later start time of the day; the latest start time's next run
    depends on the calendar and is not checked.
    """
    walls = sorted({start.hour * 60 + start.minute for start in starts})
    for start, must in zip(starts, musts, strict=True):
        wall = start.hour * 60 + start.minute
        due = must.hour * 60 + must.minute
        spelled = f"{must.hour:02d}:{must.minute:02d}"
        if due < wall:
            return (
                f"{spelled} is earlier than its start time {start.hour:02d}:{start.minute:02d}"
                " (SEM-34: the vendor refuses it; a time on the next day is written +24 hours,"
                " 10:00 as 34:00)"
            )
        later = [w for w in walls if w > wall]
        if later and due >= later[0]:
            nxt = f"{later[0] // 60:02d}:{later[0] % 60:02d}"
            return (
                f"{spelled} is not earlier than the next run's start time {nxt} (SEM-34: the"
                " vendor requires a run's must time to be earlier than the next run's start)"
            )
    return None


# ---------------------------------------------------------------- entity models (ss4)


class Time(BaseModel):
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)

    @classmethod
    def parse(cls, text: str) -> Time:
        """A time of day, 00:00-23:59 (`_parse_hhmm`)."""
        hour, minute = _parse_hhmm(text)
        return cls(hour=hour, minute=minute)


class MustTime(BaseModel):
    """SEM-34: an absolute must_start/must_complete time. The vendor's
    Limits are 00:00-71:59, "2 calendar days ahead of the current calendar
    day": hour 24-47 is the next day and 48-71 the day after (DL-253). Its
    own type, so start_times and run_window keep Time's 0-23 contract."""

    hour: int = Field(ge=0, le=71)
    minute: int = Field(ge=0, le=59)

    @classmethod
    def parse(cls, text: str) -> MustTime:
        hour, minute = _parse_hhmm(text)
        return cls(hour=hour, minute=minute)


class SlaSpec(BaseModel):
    """SEM-34: must_start/must_complete are alarms only -- annotation class,
    no control flow. Absolute times or relative '+n' offsets, never mixed."""

    model_config = ConfigDict(validate_assignment=True)

    kind: Literal["absolute", "relative"]
    times: list[MustTime] | None = None  # kind == absolute
    offsets_min: list[int] | None = None  # kind == relative; single value broadcasts

    @model_validator(mode="after")
    def _fields_match_kind(self) -> SlaSpec:
        if self.kind == "absolute" and (self.times is None or self.offsets_min is not None):
            raise ValueError("SEM-34: absolute SlaSpec carries times, not offsets")
        if self.kind == "relative" and (self.offsets_min is None or self.times is not None):
            raise ValueError("SEM-34: relative SlaSpec carries offsets_min, not times")
        return self


class ScheduleBlock(BaseModel):
    """SEM-30..35; present on JobIR iff date_conditions is truthy (SEM-30)."""

    model_config = ConfigDict(validate_assignment=True)

    days_of_week: list[str] | None = None  # XOR run_calendar (SEM-31)
    run_calendar: str | None = None
    exclude_calendar: str | None = None
    start_times: list[Time] | None = None  # XOR start_mins (SEM-31)
    start_mins: list[int] | None = None
    run_window: tuple[Time, Time] | None = None  # SEM-33: gate, NOT trigger
    timezone: str | None = None  # SEM-35
    must_start: SlaSpec | None = None  # SEM-34: annotation class
    must_complete: SlaSpec | None = None

    @model_validator(mode="after")
    def _sem31_mutual_exclusivity(self) -> ScheduleBlock:
        if self.start_times is not None and self.start_mins is not None:
            raise ValueError("SEM-31: start_times and start_mins are mutually exclusive")
        if self.days_of_week is not None and self.run_calendar is not None:
            raise ValueError("SEM-31: days_of_week and run_calendar are mutually exclusive")
        return self

    @model_validator(mode="after")
    def _start_mins_in_range(self) -> ScheduleBlock:
        for m in self.start_mins or []:
            if not 0 <= m <= 59:
                raise ValueError(f"start_mins value {m} out of range 0-59 (SEM-32)")
        return self


class BoxLinkage(BaseModel):
    box_name: str | None = None
    box_terminator: bool = False  # SEM-14
    job_terminator: bool = False


class ExecSpecBase(BaseModel):
    machine: str | None = None
    owner: str | None = None
    profile: str | None = None
    std_out_file: str | None = None
    std_err_file: str | None = None


class ExecSpec(ExecSpecBase):
    """Command jobs; FW is the analogous subclass below (ir-design ss4)."""

    kind: Literal["cmd"] = "cmd"
    command: str  # $$VAR sites kept verbatim; indexed in JobIR.var_sites
    #: 12.x sweep (DL-32): stdin redirect (may name a blob) and the job's
    #: NAME=value environment list -- CMD-only, verbatim, $$VAR-indexed.
    std_in_file: str | None = None
    envvars: str | None = None


class FwSpec(ExecSpecBase):
    """File-watcher jobs (dossier ss5): a *source* node in derived graphs."""

    kind: Literal["fw"] = "fw"
    watch_file: str
    watch_interval: int | None = None
    watch_file_min_size: int | None = None


ExecUnion = Annotated[ExecSpec | FwSpec, Field(discriminator="kind")]


# Q7 (DL-58): KB 408778 makes a present fail_codes the only verdict source, so success_codes and
# the threshold are ignored beside it; without fail_codes a present success_codes alone decides.
def exit_is_success(
    exit_code: int,
    *,
    max_exit_success: int = 0,
    success_codes: Sequence[tuple[int, int]] | None = None,
    fail_codes: Sequence[tuple[int, int]] | None = None,
) -> bool:
    """SEM-09 (DL-58): the SUCCESS or FAILURE verdict for a completion exit code.

    Shared by the oracle and the UC twin (M31). A present fail_codes decides alone: listed codes
    are FAILURE, every other code is SUCCESS, and success_codes and max_exit_success are ignored
    (KB 408778, Q7). Without fail_codes, a present success_codes replaces the default success
    rule: unlisted codes, 0 included, are FAILURE. With neither list, the max_exit_success
    threshold decides.
    """

    def _in(ranges: Sequence[tuple[int, int]]) -> bool:
        return any(lo <= exit_code <= hi for lo, hi in ranges)

    if fail_codes:
        return not _in(fail_codes)
    if success_codes is not None:
        return _in(success_codes)
    return exit_code <= max_exit_success


class CondAttr(BaseModel):
    """One condition-bearing attribute: its parsed Cond and the attribute's
    own file span, carried together (DL-73 -- they were parallel fields
    rejoined by a `{attr}_span` naming convention no type checker could see).

    Provenance (ir-design ss4: every Cond keeps a pointer to its AST span).
    Cond-node CondSpans are char offsets into the parsed text, which is
    raw_value.strip(); the scanner never leaves leading whitespace in
    raw_value, so offsets align with it. This SourceSpan locates the attr
    in the source file.
    """

    cond: Cond
    span: SourceSpan | None = None


class Semantics(BaseModel):
    """Attributes with control-flow teeth (dossier ss5)."""

    condition: CondAttr | None = None
    max_exit_success: int = 0  # SEM-09
    success_codes: list[tuple[int, int]] | None = None  # SEM-09/DL-33; CMD-only
    fail_codes: list[tuple[int, int]] | None = None  # SEM-09/DL-33; CMD-only
    term_run_time_min: int | None = None
    n_retrys: int = 0
    box_success: CondAttr | None = None  # box jobs only; SEM-12
    box_failure: CondAttr | None = None
    auto_hold: bool = False
    initial_status: InitialStatus | None = None  # SEM-24 [A]: `status:` on insert

    def exit_is_success(self, exit_code: int) -> bool:
        """SEM-09 verdict with this job's configured boundary (DL-33)."""
        return exit_is_success(
            exit_code,
            max_exit_success=self.max_exit_success,
            success_codes=self.success_codes,
            fail_codes=self.fail_codes,
        )


def _lookup_ci(mapping: dict[str, str], key: str) -> str | None:
    """Case-insensitive lookup; job passthrough keeps the raw attribute case (`Job_Load`)."""
    if key in mapping:
        return mapping[key]
    lowered = key.lower()
    for k, v in mapping.items():
        if k.lower() == lowered:
            return v
    return None


def _int_attr(mapping: dict[str, str], key: str, *, label: str) -> int | None:
    """Typed read of an opaque-carry integer attribute (job_load, priority, max_load, amount).

    None when absent; ValueError naming the text when present but not an integer. The runner's
    preflight enforces that error (DL-50): a malformed value refuses the run.
    """
    raw = _lookup_ci(mapping, key)
    if raw is None:
        return None
    text = raw.strip()
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{label} expects an integer, got {text!r}") from None


class ResourceRef(BaseModel):
    """One group of the `resources:` job attribute (DL-21; TechDocs 12.x:
    `(name, QUANTITY=n[, FREE=Y|N|A]) AND (...)`). DL-50 made this a GATE,
    not a carry: the oracle holds one capacity bucket per resource, a job
    acquires its whole demand vector atomically before RUNNING, and a short
    bucket parks it in QUE_WAIT. UCS-09 maps the same requirement to a UC
    Virtual Resource."""

    name: str
    quantity: int = Field(ge=1)
    #: Y = free units only on success, N = never freed, A = freed
    #: unconditionally; None = attribute absent (engine default NOT guessed).
    free: Literal["Y", "N", "A"] | None = None


class VarSite(BaseModel):
    """One `$$NAME` / `$${NAME}` occurrence in a string attribute (SEM-08)."""

    attr: str  # attribute that holds it, e.g. "command"
    name: str  # global variable name, no markers
    braced: bool
    start: int  # char offsets into the stored attribute value
    end: int


class JobIR(BaseModel):
    # validators re-run on field reassignment; mutating nested containers in
    # place bypasses them -- analysis passes must stay pure (ir-design ss5)
    model_config = ConfigDict(validate_assignment=True)

    name: str
    job_type: str  # normalized: 'CMD','BOX','FW', + extensible
    box: BoxLinkage = BoxLinkage()
    schedule: ScheduleBlock | None = None
    exec_: ExecUnion | None = None
    sem: Semantics = Semantics()
    annotations: dict[str, str] = {}  # alarms, notifications -- no control flow
    passthrough: dict[str, str] = {}  # known-inert / permitted attrs, verbatim
    resources: list[ResourceRef] = []  # `resources:` groups (DL-21), typed carry
    var_sites: list[VarSite] = []  # indexed $$VAR occurrences across string attrs
    span: SourceSpan | None = None

    @model_validator(mode="after")
    def _sem12_box_overrides_only_on_boxes(self) -> JobIR:
        if self.job_type != "BOX" and (
            self.sem.box_success is not None or self.sem.box_failure is not None
        ):
            raise ValueError(
                f"SEM-12: box_success/box_failure are box-job attributes;"
                f" {self.name!r} is {self.job_type}"
            )
        return self

    @model_validator(mode="after")
    def _sem09_code_sets_only_on_cmd(self) -> JobIR:
        if self.job_type != "CMD" and (
            self.sem.success_codes is not None or self.sem.fail_codes is not None
        ):
            raise ValueError(
                f"SEM-09/DL-33: success_codes/fail_codes apply to command jobs"
                f" (TechDocs: Command/i5/Micro Focus/z/OS); {self.name!r} is {self.job_type}"
            )
        return self

    def iter_conditions(
        self,
    ) -> Iterator[
        tuple[Literal["condition", "box_success", "box_failure"], Cond, SourceSpan | None]
    ]:
        """(attr_name, cond, attr_span) for each condition, box_success and box_failure set.

        The walker shared by lint's L001-family rules and derive's pass-1 extraction.
        """
        if self.sem.condition is not None:
            yield "condition", self.sem.condition.cond, self.sem.condition.span
        if self.sem.box_success is not None:
            yield "box_success", self.sem.box_success.cond, self.sem.box_success.span
        if self.sem.box_failure is not None:
            yield "box_failure", self.sem.box_failure.cond, self.sem.box_failure.span

    def job_load_units(self) -> int | None:
        """DL-50: `job_load` as the demand on the machine bucket; None is absent (demand 0;
        # PENDING: Qr4)."""
        return _int_attr(self.passthrough, "job_load", label="job_load")

    def priority_value(self) -> int | None:
        """DL-50: `priority` for QUE_WAIT waiter ordering, lower number first.

        Only a positive value makes a start check machine load (DL-247). None is unset
        (# PENDING: Qr2: where an unset priority sorts among resource waiters); load queueing
        reads unset as 0.
        """
        return _int_attr(self.passthrough, "priority", label="priority")


class MachineMember(BaseModel):
    """One component machine of a virtual machine (type v) or real-machine
    pool: a `machine:` line plus its trailing per-member `factor:`/`max_load:`
    (DL-49). Placement/load semantics stay opaque v1 -- the runner's machine
    resolver reads `name` only (factor/max_load carried, not interpreted)."""

    name: str
    attrs: dict[str, str] = {}  # per-member factor/max_load, verbatim


class MachineIR(BaseModel):
    """insert_machine record; resource/placement semantics are opaque v1
    (dossier ss5), so everything beyond the name/type/members is carried
    verbatim. `members` holds the repeated `machine:` component lines a
    virtual machine / real pool declares (DL-49); machine-level attributes
    (node_name, ...) stay in `attrs`. Non-pool machines have members=[]."""

    name: str
    machine_type: str | None = None  # JIL `type:` -- r(eal)/v(irtual)/a(gent), verbatim
    members: list[MachineMember] = []
    attrs: dict[str, str] = {}
    span: SourceSpan | None = None

    def max_load_units(self) -> int | None:
        """DL-50: machine-level `max_load`, the machine-load bucket capacity.

        None is unset (no throttle) or a pool (type v or real pool): per-member load placement is
        unmodelled (DL-49), so pools carry no machine-load throttle (# PENDING: Qr3). Resource
        semaphores still apply to jobs pinned to them.
        """
        if self.members:
            return None
        return _int_attr(self.attrs, "max_load", label="max_load")


class ResourceIR(BaseModel):
    """insert_resource record (virtual resources); carried opaquely v1 like
    MachineIR (DL-18) -- UCS-09/M34 map these to UC Virtual Resources when the
    backend lands, and typing amount/res_type before a consumer exists would
    be speculation."""

    # The class docstring is in the pinned IR-F schema (arch_check.IR_SCHEMA_PIN) and stays as is.
    # `res_type` is carried verbatim. classify, capacity and preflight normalise it through
    # `capacity.resource_type`; equiv compares it as written. A malformed `amount` or an unknown
    # `res_type` is refused at preflight, not at lowering (DL-50).
    name: str
    res_type: str | None = None  # JIL `res_type:` -- verbatim
    attrs: dict[str, str] = {}
    span: SourceSpan | None = None

    def capacity_units(self) -> int | None:
        """DL-50: `amount`, the resource bucket's total capacity; None when not declared.

        Preflight refuses a job that requests an unsized resource, which is stricter than L016.
        """
        return _int_attr(self.attrs, "amount", label="amount")


class XinstIR(BaseModel):
    """insert_xinst record (SEM-07 boundary marker). xtype stays typed -- it
    selects the protocol family and is the one attribute conditions depend
    on; the connection plumbing (xmachine -- required by TechDocs 12.x --
    xport, xmanager, xcrypt_type, xkey_to_manager) is carried verbatim like
    MachineIR: boundary records, not job semantics (DL-28)."""

    name: str
    xtype: str
    attrs: dict[str, str] = {}
    span: SourceSpan | None = None


class CalendarIR(BaseModel):
    """autocal_asc export record (DL-36): a standard `calendar:` (bare date
    rows) or `extended_calendar:`/`ext_calendar:` (rule attributes)
    definition. Carried opaquely like MachineIR (DL-18); the runner's
    autocal interpreter (DL-57) consumes the carry -- the compiler still
    never expands rules. `condition:` lines are legally repeatable
    (SEM-36) and land in `conditions` in source order; every other key
    stays unique-or-error."""

    name: str
    kind: Literal["standard", "extended"]
    attrs: dict[str, str] = {}
    conditions: list[str] = []  # extended: `condition:` lines, stripped, in order
    dates: list[str] = []  # standard calendars: date rows, stripped verbatim
    span: SourceSpan | None = None


class CycleIR(BaseModel):
    """autocal_asc `cycle:` record (DL-36): a named date-range set that
    extended calendars reference via cyccal. Opaque carry like CalendarIR;
    repeated `start_date:`/`end_date:` pairs (one per period, SEM-39) land
    in `periods` in source order."""

    name: str
    attrs: dict[str, str] = {}
    periods: list[tuple[str, str]] = []  # (start_date, end_date) rows, stripped
    span: SourceSpan | None = None


class CatalogMeta(BaseModel):
    source_files: list[str] = []
    tool_version: str = ""
    parsed_at: str | None = None  # caller-stamped; None keeps dumps deterministic


_BOX_DEPTH_SANITY = 64  # ir-design ss4: depth at most 64; SEM-17: nesting is legal


def _box_tree_problems(jobs: dict[str, JobIR]) -> list[tuple[str, str]]:
    """(member_name, message) pairs for the CatalogIR validator and for lowering, which adds spans.

    Membership problems and cycles are reported together; a link already reported as missing or
    non-box is not reported again.
    """
    problems: list[tuple[str, str]] = []
    for name, job in jobs.items():
        target_name = job.box.box_name
        if target_name is None:
            continue
        target = jobs.get(target_name)
        if target is None:
            problems.append(
                (name, f"{name!r}: box_name {target_name!r} is not defined in the catalog")
            )
        elif target.job_type != "BOX":
            problems.append(
                (name, f"{name!r}: box_name {target_name!r} is a {target.job_type} job, not a box")
            )
    reported_cycles: set[frozenset[str]] = set()
    for name, job in jobs.items():
        chain = [name]
        depth = 0
        current = job
        while (up := current.box.box_name) is not None:
            nxt = jobs.get(up)
            if nxt is None:
                break  # dangling link: reported by the membership pass above
            if up in chain:
                cycle = frozenset(chain[chain.index(up) :])
                if cycle not in reported_cycles:
                    reported_cycles.add(cycle)
                    problems.append((name, "box containment cycle: " + " -> ".join(sorted(cycle))))
                break
            depth += 1
            if depth > _BOX_DEPTH_SANITY:
                problems.append((name, f"box nesting deeper than {_BOX_DEPTH_SANITY} at {name!r}"))
                break
            chain.append(up)
            current = nxt
    return problems


class CatalogIR(BaseModel):
    """The compilation unit. Identity is by name, sys_id-free (ir-design ss8)."""

    model_config = ConfigDict(validate_assignment=True)

    ir_version: Literal["0.3"] = IR_VERSION
    jobs: dict[str, JobIR] = {}
    globals_declared: dict[str, str] = {}  # insert_global
    external_instances: dict[str, XinstIR] = {}  # insert_xinst (SEM-07), plumbing opaque (DL-28)
    machines: dict[str, MachineIR] = {}
    resources: dict[str, ResourceIR] = {}  # insert_resource, opaque v1 (DL-18)
    calendars: dict[str, CalendarIR] = {}  # standard + extended share the run_calendar namespace
    cycles: dict[str, CycleIR] = {}  # referenced by extended calendars' cyccal (DL-36)
    meta: CatalogMeta = CatalogMeta()

    @model_validator(mode="after")
    def _box_tree_is_sound(self) -> CatalogIR:
        problems = _box_tree_problems(self.jobs)
        if problems:
            raise ValueError("; ".join(msg for _, msg in problems))
        return self


# ---------------------------------------------------------------- serialization (ss8)


def dump_catalog(catalog: CatalogIR) -> str:
    """Deterministic JSON: sorted keys, stable indent, trailing newline (ir-design ss8)."""
    return json.dumps(catalog.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"


def load_catalog(text: str) -> CatalogIR:
    return CatalogIR.model_validate(json.loads(text))


def tool_version() -> str:
    try:
        return metadata.version("dsl41")
    except metadata.PackageNotFoundError:
        return "0+unknown"


# ------------------------------------------------------------- lowering (AST -> IR-F)


class LoweringFinding(BaseModel):
    message: str
    span: SourceSpan | None = None

    def render(self) -> str:
        if self.span is None:
            return self.message
        return f"{self.span.at}: {self.message}"


class LoweringError(ValueError):
    """Loud, classified lowering failure (DL-04/DL-07); carries every finding."""

    def __init__(self, findings: list[LoweringFinding]) -> None:
        super().__init__(
            f"{len(findings)} lowering error(s):\n"
            + "\n".join(f"  - {f.render()}" for f in findings)
        )
        self.findings = findings


_SUPPORTED_SUBCOMMANDS = {
    "insert_job",
    "insert_global",
    "insert_machine",
    "insert_xinst",
    "insert_resource",
    # DL-36: autocal_asc calendar-export statements; ext_calendar is the Manage Calendars
    # spelling of the same record (SEM-36, Q9 default: accept both, DL-57)
    "calendar",
    "cycle",
    "extended_calendar",
    "ext_calendar",
}

#: Exec-cluster attributes valid on both CMD and FW jobs (ExecSpecBase).
#: Public: dsl.py's decompiler imports this five-name cluster (DL-178).
EXEC_BASE_ATTRS = ("machine", "owner", "profile", "std_out_file", "std_err_file")

#: Exec-shaped attributes that are inert on a BOX (boxes do not execute,
#: SEM-10) and route to passthrough verbatim; the CMD-only pair joins the base cluster (DL-32).
_BOX_INERT_ATTRS = frozenset(EXEC_BASE_ATTRS) | {"std_in_file", "envvars"}


_WRAPPED_QUOTES_RE = re.compile(r'"[^"]*"')


def unquote_jil_value(value: str) -> str:
    """Semantic unquoting for the typed lane (jil-statement-syntax rule 7).

    Exactly one wrapping quote pair with no interior quotes comes off. Anything else, such as
    partial quoting or shell syntax inside a command, is returned stripped but verbatim.

    The one public owner of the rule (DL-178q). It produces the calendar catalog key
    (`_lower_calendar`, `_lower_cycle`), and autocal's holcal/cyccal resolution, classify's
    `_named_ref` and L018 call it, so lookup and production use one predicate.
    """
    v = value.strip()
    if _WRAPPED_QUOTES_RE.fullmatch(v):
        return v[1:-1]
    return v


#: An exit code or lo-hi range with a negative end: `-1`, `-5-3`, `-5--1`, `1--3`. Other
#: malformed tokens do not match and get the ordinary malformed-token message.
_NEGATIVE_CODE_RE = re.compile(r"-\d+(?:--?\d+)?|\d+--\d+")


def _split_list(value: str) -> list[str]:
    """Comma-separated JIL list; tolerates rule-6 newlines and empty segments."""
    return [t for t in (part.strip() for part in unquote_jil_value(value).split(",")) if t]


#: Attribute names whose values lowering funnels through `unquote_jil_value` (directly or via
#: `_split_list`), across every statement kind: insert_job, and the `type`, `res_type` and `xtype`
#: of insert_machine, insert_resource and insert_xinst. The DSL builder auto-quotes only these
#: lanes when a bare value would open a rule-5 block comment (DL-161), and lowering strips the
#: quote, so the round trip is exact. Every other lane stores bytes verbatim and refuses instead.
#: The name follows the mechanism, not the statement kind: `_BOX_INERT_ATTRS` and `TIME_CLUSTER`
#: also route through `unquote_jil_value`, plus nine names in no other named cluster, three of
#: them not job attrs.
UNQUOTED_AT_LOWERING = (
    _BOX_INERT_ATTRS
    | TIME_CLUSTER
    | frozenset(
        {
            "command",
            "watch_file",
            "box_name",
            "status",
            "success_codes",
            "fail_codes",
            "type",
            "res_type",
            "xtype",
        }
    )
)


def render_code_ranges(ranges: list[tuple[int, int]], *, sep: str = ", ") -> str:
    """SEM-09/DL-33 exit-code sets in surface form; lowering keeps the author's partition sorted
    and unmerged, so render(parse(x)) is stable."""
    return sep.join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in ranges)


def find_var_sites(attr: str, value: str) -> list[VarSite]:
    """Index `$$NAME` / `$${NAME}` sites in one stored attribute value (SEM-08)."""
    sites: list[VarSite] = []
    for m in _VAR_RE.finditer(value):
        braced = m.group(1) is not None
        sites.append(
            VarSite(
                attr=attr,
                name=m.group(1) or m.group(2),
                braced=braced,
                start=m.start(),
                end=m.end(),
            )
        )
    return sites


class _Lowerer:
    def __init__(self, permit_unknown: bool) -> None:
        self.permit_unknown = permit_unknown
        self.findings: list[LoweringFinding] = []
        self.jobs: dict[str, JobIR] = {}
        self.globals_declared: dict[str, str] = {}
        self.external_instances: dict[str, XinstIR] = {}
        self.machines: dict[str, MachineIR] = {}
        self.resources: dict[str, ResourceIR] = {}
        self.calendars: dict[str, CalendarIR] = {}
        self.cycles: dict[str, CycleIR] = {}
        self.source_files: list[str] = []

    def err(self, message: str, span: SourceSpan | None) -> None:
        self.findings.append(LoweringFinding(message=message, span=span))

    def _check_scalar_strings(self, stmt: JilStatement) -> None:
        """PR-10a: refuse a statement that carries an unpaired surrogate anywhere.

        Such a statement cannot be canonicalized, so the estate could never be sealed
        (period-model ss3.2). The check covers the statement's whole dump, so a new AST field is
        covered by default (DL-83).
        """
        if not is_scalar_json(stmt.model_dump(mode="json")):
            self.err(
                f"statement {stmt.subject!r} carries an unpaired surrogate;"
                " not a Unicode scalar string (PR-10a)",
                stmt.span,
            )

    # ------------------------------------------------------------------ driver

    def run(self, files: Iterable[JilFile]) -> CatalogIR:
        for jf in files:
            if not is_scalar_string(jf.file):
                # PR-10a: the path rides in SourceSpan.file, so in the catalog hash
                self.err(f"source path {jf.file!r} carries an unpaired surrogate (PR-10a)", None)
            self.source_files.append(jf.file)
            for stmt in jf.statements:
                self._check_scalar_strings(stmt)
                sub = stmt.subcommand.lower()
                if sub == "insert_job":
                    self._lower_job(stmt)
                elif sub == "insert_global":
                    self._lower_global(stmt)
                elif sub == "insert_machine":
                    self._lower_machine(stmt)
                elif sub == "insert_resource":
                    self._lower_resource(stmt)
                elif sub == "insert_xinst":
                    self._lower_xinst(stmt)
                elif sub == "calendar":
                    self._lower_calendar(stmt, kind="standard")
                elif sub in ("extended_calendar", "ext_calendar"):
                    self._lower_calendar(stmt, kind="extended")
                elif sub == "cycle":
                    self._lower_cycle(stmt)
                else:
                    self.err(
                        f"subcommand {stmt.subcommand!r} is not supported by lowering v1"
                        " (supported: " + ", ".join(sorted(_SUPPORTED_SUBCOMMANDS)) + ");"
                        " update/delete/override/rename merging is semantics, not syntax,"
                        " and monitor/report, job-type, blob/glob, and connection-profile"
                        " objects are out of compile scope (DL-29)",
                        stmt.span,
                    )
        for name, message in _box_tree_problems(self.jobs):
            self.err(message, self.jobs[name].span)
        if self.findings:
            raise LoweringError(self.findings)
        return CatalogIR(
            jobs=self.jobs,
            globals_declared=self.globals_declared,
            external_instances=self.external_instances,
            machines=self.machines,
            resources=self.resources,
            calendars=self.calendars,
            cycles=self.cycles,
            meta=CatalogMeta(source_files=self.source_files, tool_version=tool_version()),
        )

    # ------------------------------------------------------------- shared helpers

    def _fresh_attr(self, seen: Container[str], attr: RawAttr) -> bool:
        """True for a fresh key. A repeat emits the duplicate-attribute error and returns False.

        One owner for the four source-order lowerers that walk `stmt.attrs` (DL-178).
        """
        if attr.key.lower() in seen:
            self.err(f"duplicate attribute {attr.key!r} in one statement", attr.span)
            return False
        return True

    def _collect_attrs(self, stmt: JilStatement) -> dict[str, RawAttr] | None:
        """Key-lowered attr map; a duplicate key is a lowering error."""
        attrs: dict[str, RawAttr] = {}
        ok = True
        for attr in stmt.attrs:
            if not self._fresh_attr(attrs, attr):
                ok = False
                continue
            attrs[attr.key.lower()] = attr
        return attrs if ok else None

    def _subject(self, stmt: JilStatement, what: str) -> str | None:
        name = stmt.subject.strip()
        if not name:
            self.err(f"{stmt.subcommand}: missing {what} name", stmt.span)
            return None
        return name

    def _int_attr(self, attr: RawAttr) -> int | None:
        try:
            return int(attr.raw_value.strip())
        except ValueError:
            self.err(f"{attr.key}: expected an integer, got {attr.raw_value.strip()!r}", attr.span)
            return None

    def _bool_attr(self, attr: RawAttr) -> bool | None:
        v = attr.raw_value.strip().lower()
        if v in _TRUTHY:
            return True
        if v in _FALSY:
            return False
        self.err(
            f"{attr.key}: expected a boolean (0/1/n/y), got {attr.raw_value.strip()!r}", attr.span
        )
        return None

    def _code_set_attr(self, attr: RawAttr) -> list[tuple[int, int]] | None:
        """SEM-09/DL-33 exit-code sets: a comma list of codes and lo-hi ranges ('1,3,20-30').

        Sorted, never merged: the partition is the author's, and only membership is semantics.
        """
        ranges: list[tuple[int, int]] = []
        for token in _split_list(attr.raw_value):
            if _NEGATIVE_CODE_RE.fullmatch(token):
                # a well-formed code or range with a negative end gets its own message
                self.err(
                    f"{attr.key}: negative exit code in {token!r} is not supported: on"
                    " POSIX an exit status is 0-255, and a job killed by a signal is"
                    " TERMINATED with no exit code, so a negative code can never match"
                    " (SEM-09)",
                    attr.span,
                )
                return None
            lo_text, sep, hi_text = token.partition("-")
            try:
                lo = int(lo_text)
                hi = int(hi_text) if sep else lo
            except ValueError:
                self.err(
                    f"{attr.key}: expected an exit code or lo-hi range, got {token!r}"
                    " (SEM-09/DL-33)",
                    attr.span,
                )
                return None
            if hi < lo:
                self.err(f"{attr.key}: empty range {token!r} (lo > hi)", attr.span)
                return None
            ranges.append((lo, hi))
        if not ranges:
            self.err(f"{attr.key}: empty value", attr.span)
            return None
        return sorted(ranges)

    def _cond_attr(self, attr: RawAttr) -> CondAttr | None:
        try:
            return CondAttr(cond=parse_condition(attr.raw_value.strip()), span=attr.span)
        except ConditionParseError as exc:
            self.err(f"{attr.key}: {exc}", attr.span)
            return None

    # ------------------------------------------------------------------ insert_job

    _RESOURCE_GROUP_RE = re.compile(r"\(([^()]*)\)")

    def _parse_resources(self, attr: RawAttr) -> list[ResourceRef]:
        """`(name, QUANTITY=n[, FREE=Y|N|A]) AND (...)` (DL-21); keywords are case-insensitive.

        Anything else is a lowering error, because guessing a resource gate would be silent loss.
        """

        def bad(why: str) -> None:
            self.err(
                f"resources: {why} (expected '(name, QUANTITY=n[, FREE=Y|N|A]) AND (...)'; DL-21)",
                attr.span,
            )

        raw = attr.raw_value.strip()
        groups = self._RESOURCE_GROUP_RE.findall(raw)
        separators = [t for t in self._RESOURCE_GROUP_RE.sub(" ", raw).split() if t]
        if not groups:
            bad(f"no '(name, ...)' group in {raw!r}")
            return []
        if len(separators) != len(groups) - 1 or any(s.lower() != "and" for s in separators):
            bad(f"groups must be joined by AND, got {raw!r}")
            return []
        refs: list[ResourceRef] = []
        for group in groups:
            parts = [p.strip() for p in group.split(",")]
            name = parts[0]
            if not name or "=" in name:
                bad(f"group {group!r} must start with a resource name")
                continue
            quantity: int | None = None
            free: Literal["Y", "N", "A"] | None = None
            ok = True
            for part in parts[1:]:
                key, sep, value = part.partition("=")
                key, value = key.strip().upper(), value.strip().upper()
                if not sep:
                    bad(f"expected KEY=VALUE, got {part!r}")
                    ok = False
                elif key == "QUANTITY":
                    try:
                        quantity = int(value)
                    except ValueError:
                        bad(f"QUANTITY expects an integer, got {value!r}")
                        ok = False
                elif key == "FREE":
                    if value in ("Y", "N", "A"):
                        free = cast(Literal["Y", "N", "A"], value)
                    else:
                        bad(f"FREE expects Y, N, or A, got {value!r}")
                        ok = False
                else:
                    bad(f"unknown keyword {key!r} in group {group!r}")
                    ok = False
            if quantity is None:
                # every example carries QUANTITY; defaulting would be a guess
                bad(f"group {group!r} is missing QUANTITY")
                ok = False
            elif quantity < 1:
                bad(f"QUANTITY must be >= 1, got {quantity}")
                ok = False
            if ok:
                assert quantity is not None
                refs.append(ResourceRef(name=name, quantity=quantity, free=free))
        return refs

    def _lower_job(self, stmt: JilStatement) -> None:
        name = self._subject(stmt, "job")
        if name is not None:
            # DL-39: the catalog key is the semantic name, `\:` unescaped as in condition refs
            name = unescape_job_name(name)
        if name is not None and name in self.jobs:
            first = self.jobs[name].span
            origin = first.at if first is not None else "unknown"
            self.err(
                f"duplicate job name {name!r} in compilation set; first defined at {origin}",
                stmt.span,
            )
            return
        attrs = self._collect_attrs(stmt)
        if name is None or attrs is None:
            return
        job_type = self._job_type(stmt, attrs)
        if job_type is None:
            return
        box = self._box_linkage(attrs)
        sem = self._semantics(attrs)
        schedule = self._schedule(attrs)
        exec_ = self._exec_spec(job_type, attrs)
        resources: list[ResourceRef] = []
        if (attr := attrs.pop("resources", None)) is not None:
            resources = self._parse_resources(attr)
        annotations: dict[str, str] = {}
        passthrough: dict[str, str] = {}
        if schedule is None:
            # SEM-30: time attrs without truthy date_conditions are dead config, carried verbatim
            # (the falsy switch included) for L005
            for key in sorted((TIME_CLUSTER | {"date_conditions"}) & attrs.keys()):
                attr = attrs.pop(key)
                passthrough[attr.key] = attr.raw_value.strip()
        inert_for_type = _BOX_INERT_ATTRS if job_type == "BOX" else frozenset()
        for key in sorted(attrs.keys()):
            attr = attrs[key]
            if key in ANNOTATION_ATTRS:
                annotations[attr.key] = attr.raw_value.strip()
            elif key in PASSTHROUGH_ALLOWED or key in inert_for_type or self.permit_unknown:
                passthrough[attr.key] = attr.raw_value.strip()
            else:
                self.err(
                    f"unknown attribute {attr.key!r} is not on the inert allow-list;"
                    " refusing to guess its semantics (DL-07; use --permit-unknown"
                    " to carry it verbatim)",
                    attr.span,
                )
        var_sites: list[VarSite] = []
        if exec_ is not None:
            exec_fields = exec_.model_dump(exclude={"kind"}, exclude_none=True)
            for field_name, value in exec_fields.items():
                if isinstance(value, str):
                    var_sites.extend(find_var_sites(field_name, value))
        for source in (annotations, passthrough):
            for attr_name, value in source.items():
                var_sites.extend(find_var_sites(attr_name, value))
        try:
            self.jobs[name] = JobIR(
                name=name,
                job_type=job_type,
                box=box,
                schedule=schedule,
                exec_=exec_,
                sem=sem,
                annotations=annotations,
                passthrough=passthrough,
                resources=resources,
                var_sites=var_sites,
                span=stmt.span,
            )
        except ValidationError as exc:
            for e in exc.errors():
                self.err(f"{name}: {e['msg']}", stmt.span)

    def _job_type(self, stmt: JilStatement, attrs: dict[str, RawAttr]) -> str | None:
        inline = stmt.job_type_inline.strip() if stmt.job_type_inline is not None else None
        attr = attrs.pop("job_type", None)
        raw = inline
        if attr is not None:
            attr_val = attr.raw_value.strip()
            if inline is not None and attr_val.lower() != inline.lower():
                self.err(
                    f"conflicting job_type: inline {inline!r} vs attribute {attr_val!r}",
                    attr.span,
                )
                return None
            raw = attr_val if inline is None else inline
        if raw is None:
            self.err("job_type is required (see ir.py module docstring)", stmt.span)
            return None
        normalized = _JOB_TYPE_MAP.get(raw.lower())
        if normalized is None:
            self.err(
                f"job_type {raw!r} is not supported by lowering v1 (CMD/BOX/FW);"
                " refusing to guess semantics for other types",
                stmt.span,
            )
            return None
        return normalized

    def _box_linkage(self, attrs: dict[str, RawAttr]) -> BoxLinkage:
        box_name = None
        if (attr := attrs.pop("box_name", None)) is not None:
            # DL-39: the same semantic-name funnel as the job subject, so the linkage joins the key
            box_name = unescape_job_name(unquote_jil_value(attr.raw_value))
            if not box_name:
                self.err("box_name: empty value", attr.span)
                box_name = None
        box_terminator = job_terminator = False
        if (attr := attrs.pop("box_terminator", None)) is not None:
            box_terminator = self._bool_attr(attr) or False
        if (attr := attrs.pop("job_terminator", None)) is not None:
            job_terminator = self._bool_attr(attr) or False
        return BoxLinkage(
            box_name=box_name, box_terminator=box_terminator, job_terminator=job_terminator
        )

    def _semantics(self, attrs: dict[str, RawAttr]) -> Semantics:
        sem = Semantics()
        if (attr := attrs.pop("condition", None)) is not None:
            sem.condition = self._cond_attr(attr)
        if (attr := attrs.pop("box_success", None)) is not None:
            sem.box_success = self._cond_attr(attr)
        if (attr := attrs.pop("box_failure", None)) is not None:
            sem.box_failure = self._cond_attr(attr)
        if (attr := attrs.pop("max_exit_success", None)) is not None:
            sem.max_exit_success = self._int_attr(attr) or 0
        if (attr := attrs.pop("success_codes", None)) is not None:
            sem.success_codes = self._code_set_attr(attr)
        if (attr := attrs.pop("fail_codes", None)) is not None:
            sem.fail_codes = self._code_set_attr(attr)
        if (attr := attrs.pop("term_run_time", None)) is not None:
            sem.term_run_time_min = self._int_attr(attr)
        if (attr := attrs.pop("n_retrys", None)) is not None:
            sem.n_retrys = self._int_attr(attr) or 0
        if (attr := attrs.pop("auto_hold", None)) is not None:
            sem.auto_hold = self._bool_attr(attr) or False
        if (attr := attrs.pop("status", None)) is not None:
            value = unquote_jil_value(attr.raw_value).upper()
            if value in _INITIAL_STATUSES:
                sem.initial_status = cast(InitialStatus, value)
            else:
                self.err(
                    f"status: {attr.raw_value.strip()!r} is not a modeled definition-time"
                    " status (SEM-24 models INACTIVE/ON_HOLD/ON_ICE/ON_NOEXEC; run states"
                    " would interact with the SEM-01 latch -- refusing to guess)",
                    attr.span,
                )
        return sem

    # ------------------------------------------------------------------- schedule

    def _schedule(self, attrs: dict[str, RawAttr]) -> ScheduleBlock | None:
        dc = attrs.get("date_conditions")
        if dc is None:
            return None
        truthy = self._bool_attr(dc)
        if truthy is None:
            attrs.pop("date_conditions")  # finding recorded; not passthrough
            return None
        if not truthy:
            return None  # falsy stays in attrs, then passthrough (SEM-30)
        attrs.pop("date_conditions")
        fields: dict[str, object] = {}
        spans: dict[str, SourceSpan] = {}

        def take(key: str) -> RawAttr | None:
            attr = attrs.pop(key, None)
            if attr is not None:
                spans[key] = attr.span
            return attr

        if (attr := take("days_of_week")) is not None:
            fields["days_of_week"] = self._days_of_week(attr)
        if (attr := take("run_calendar")) is not None:
            fields["run_calendar"] = unquote_jil_value(attr.raw_value)
        if (attr := take("exclude_calendar")) is not None:
            fields["exclude_calendar"] = unquote_jil_value(attr.raw_value)
        if (attr := take("timezone")) is not None:
            fields["timezone"] = unquote_jil_value(attr.raw_value)
        if (attr := take("start_times")) is not None:
            fields["start_times"] = self._times_attr(attr)
        if (attr := take("start_mins")) is not None:
            fields["start_mins"] = self._ints_attr(attr)
        if (attr := take("run_window")) is not None:
            fields["run_window"] = self._run_window(attr)
        start_times = fields.get("start_times")
        starts = start_times if isinstance(start_times, list) else None
        by_mins = starts is None and isinstance(fields.get("start_mins"), list)
        if (attr := take("must_start_times")) is not None:
            fields["must_start"] = self._sla_attr(attr, starts, by_mins=by_mins)
        if (attr := take("must_complete_times")) is not None:
            fields["must_complete"] = self._sla_attr(attr, starts, by_mins=by_mins)
        # SEM-31 pre-check, by presence, so findings point at the attribute line; the model
        # validator stays the ground truth
        ok = True
        if "start_times" in fields and "start_mins" in fields:
            self.err(
                "schedule: SEM-31: start_times and start_mins are mutually exclusive",
                spans["start_mins"],
            )
            ok = False
        if "days_of_week" in fields and "run_calendar" in fields:
            self.err(
                "schedule: SEM-31: days_of_week and run_calendar are mutually exclusive",
                spans["run_calendar"],
            )
            ok = False
        if not ok:
            return None
        try:
            return ScheduleBlock(**{k: v for k, v in fields.items() if v is not None})  # type: ignore[arg-type]
        except ValidationError as exc:
            for e in exc.errors():
                self.err(f"schedule: {e['msg']}", dc.span)
            return None

    def _days_of_week(self, attr: RawAttr) -> list[str] | None:
        days: list[str] = []
        for token in _split_list(attr.raw_value):
            t = _DAY_FULL.get(token.lower(), token.lower())
            if t not in _DAY_TOKENS:
                self.err(f"days_of_week: unknown day token {token!r}", attr.span)
                return None
            days.append(t)
        if not days:
            self.err("days_of_week: empty value", attr.span)
            return None
        return days

    def _times_attr(self, attr: RawAttr) -> list[Time] | None:
        times: list[Time] = []
        for token in _split_list(attr.raw_value):
            try:
                times.append(Time.parse(token))
            except ValueError as exc:
                self.err(f"{attr.key}: {exc}", attr.span)
                return None
        if not times:
            self.err(f"{attr.key}: empty value", attr.span)
            return None
        return times

    def _ints_attr(self, attr: RawAttr) -> list[int] | None:
        values: list[int] = []
        for token in _split_list(attr.raw_value):
            try:
                values.append(int(token))
            except ValueError:
                self.err(f"{attr.key}: expected an integer, got {token!r}", attr.span)
                return None
        if not values:
            self.err(f"{attr.key}: empty value", attr.span)
            return None
        return values

    def _run_window(self, attr: RawAttr) -> tuple[Time, Time] | None:
        parts = unquote_jil_value(attr.raw_value).split("-")
        if len(parts) != 2:
            self.err(
                f'run_window: expected "HH:MM-HH:MM", got {attr.raw_value.strip()!r}', attr.span
            )
            return None
        try:
            return (Time.parse(parts[0]), Time.parse(parts[1]))
        except ValueError as exc:
            self.err(f"run_window: {exc}", attr.span)
            return None

    def _sla_attr(
        self, attr: RawAttr, starts: list[Time] | None, *, by_mins: bool = False
    ) -> SlaSpec | None:
        """SEM-34 must_*_times: absolute or relative, never mixed.

        The count matches start_times, except a single relative offset broadcasts (DL-248).
        Against start_mins only one relative offset lowers, broadcast to every tick (DL-248). The
        vendor refuses an absolute form there (DL-253). A list of relative offsets there is
        unspecified and refused too.
        """
        n_start_times = None if starts is None else len(starts)
        tokens = _split_list(attr.raw_value)
        if not tokens:
            self.err(f"{attr.key}: empty value", attr.span)
            return None
        relative = [t.startswith("+") for t in tokens]
        if any(relative) and not all(relative):
            self.err(f"{attr.key}: absolute and relative forms cannot be mixed (SEM-34)", attr.span)
            return None
        if by_mins:
            if not all(relative):
                self.err(
                    f"{attr.key}: absolute times require start_times; the vendor refuses"
                    " them with start_mins (SEM-34, DL-253)",
                    attr.span,
                )
                return None
            if len(tokens) != 1:
                self.err(
                    f"{attr.key}: with start_mins only a single relative offset is accepted"
                    " (SEM-34, DL-248); a list of offsets against start_mins is not"
                    " specified by the vendor and stays open",
                    attr.span,
                )
                return None
            n_start_times = 1
        if n_start_times is None:
            self.err(f"{attr.key}: requires start_times or start_mins (SEM-34)", attr.span)
            return None
        if all(relative):
            offsets: list[int] = []
            for t in tokens:
                # SEM-34's relative form is a plus and decimal digits; int() alone also takes signs,
                # underscores and non-ASCII digits (DL-151). Vendor range limits are not checked
                if _REL_OFFSET_RE.fullmatch(t) is None:
                    self.err(
                        f"{attr.key}: invalid relative offset {t!r}"
                        " (SEM-34: '+' followed by decimal digits)",
                        attr.span,
                    )
                    return None
                offsets.append(int(t[1:]))
            if len(offsets) not in (1, n_start_times):
                self.err(
                    f"{attr.key}: {len(offsets)} offsets for {n_start_times} start_times"
                    " (SEM-34: count must match; a single offset broadcasts)",
                    attr.span,
                )
                return None
            return SlaSpec(kind="relative", offsets_min=offsets)
        times: list[MustTime] = []
        for t in tokens:
            # SEM-34: the vendor's limits are 00:00-71:59 (DL-253). The hour is checked before
            # MustTime's own bound, so the message names the limit
            try:
                hour, minute = _parse_hhmm(t)
                if hour > 71:
                    self.err(
                        f"{attr.key}: {t.strip()}: hour {hour} is outside the"
                        " vendor's Limits of 00:00-71:59 for the absolute form (SEM-34: two"
                        " calendar days ahead of the current calendar day)",
                        attr.span,
                    )
                    return None
                times.append(MustTime(hour=hour, minute=minute))
            except ValueError as exc:
                self.err(f"{attr.key}: {exc}", attr.span)
                return None
        if len(times) != n_start_times:
            self.err(
                f"{attr.key}: {len(times)} times for {n_start_times} start_times"
                " (SEM-34: count must match)",
                attr.span,
            )
            return None
        assert starts is not None  # the count matched a start_times list
        if (problem := _must_time_order(starts, times)) is not None:
            self.err(f"{attr.key}: {problem}", attr.span)
            return None
        return SlaSpec(kind="absolute", times=times)

    # ----------------------------------------------------------------- exec specs

    def _exec_spec(self, job_type: str, attrs: dict[str, RawAttr]) -> ExecSpec | FwSpec | None:
        command = attrs.pop("command", None)
        watch_file = attrs.pop("watch_file", None)
        watch_interval = attrs.pop("watch_interval", None)
        watch_min_size = attrs.pop("watch_file_min_size", None)
        if job_type == "BOX":
            # Boxes do not execute (SEM-10): command and watch_* on a box are errors. The base
            # cluster (machine, owner, ...) is inert on a box: it stays in `attrs` and the
            # classifier routes it to passthrough with its spans
            for wrong in (command, watch_file, watch_interval, watch_min_size):
                if wrong is not None:
                    self.err(f"{wrong.key}: not valid on a BOX job", wrong.span)
            return None
        base: dict[str, str] = {}
        for field_name in EXEC_BASE_ATTRS:
            if (attr := attrs.pop(field_name, None)) is not None:
                base[field_name] = unquote_jil_value(attr.raw_value)
        if job_type == "CMD":
            for wrong in (watch_file, watch_interval, watch_min_size):
                if wrong is not None:
                    self.err(f"{wrong.key}: file-watcher attribute on a CMD job", wrong.span)
            if command is None:
                self.err("CMD job requires a command", None)
                return None
            std_in = attrs.pop("std_in_file", None)
            envvars = attrs.pop("envvars", None)
            return ExecSpec.model_validate(
                {
                    "command": unquote_jil_value(command.raw_value),
                    "std_in_file": unquote_jil_value(std_in.raw_value)
                    if std_in is not None
                    else None,
                    "envvars": unquote_jil_value(envvars.raw_value)
                    if envvars is not None
                    else None,
                    **base,
                }
            )
        if command is not None:  # FW
            self.err("command: not valid on an FW job", command.span)
        for cmd_only in ("std_in_file", "envvars"):  # DL-32: CMD-only exec attrs
            if (wrong := attrs.pop(cmd_only, None)) is not None:
                self.err(f"{wrong.key}: not valid on an FW job", wrong.span)
        if watch_file is None:
            self.err("FW job requires watch_file", None)
            return None
        interval = self._int_attr(watch_interval) if watch_interval is not None else None
        min_size = self._int_attr(watch_min_size) if watch_min_size is not None else None
        return FwSpec.model_validate(
            {
                "watch_file": unquote_jil_value(watch_file.raw_value),
                "watch_interval": interval,
                "watch_file_min_size": min_size,
                **base,
            }
        )

    # ------------------------------------------------- other supported subcommands

    def _lower_global(self, stmt: JilStatement) -> None:
        name = self._subject(stmt, "global variable")
        attrs = self._collect_attrs(stmt)
        if name is None or attrs is None:
            return
        if name in self.globals_declared:
            self.err(f"duplicate insert_global {name!r}", stmt.span)
            return
        value = attrs.pop("value", None)
        for leftover in attrs.values():
            self.err(f"insert_global: unexpected attribute {leftover.key!r}", leftover.span)
        if value is None:
            self.err(f"insert_global {name!r}: missing value attribute", stmt.span)
            return
        # unquoted so it compares equal to unquoted value() condition comparands
        self.globals_declared[name] = unquote_jil_value(value.raw_value)

    #: Per-member pool attributes that repeat inside one insert_machine, once per component
    #: machine (DL-49). Any other duplicate key is a lowering error.
    _MACHINE_MEMBER_ATTRS = frozenset({"factor", "max_load"})

    def _lower_machine(self, stmt: JilStatement) -> None:
        name = self._subject(stmt, "machine")
        if name is None:
            return
        if name in self.machines:
            self.err(f"duplicate insert_machine {name!r}", stmt.span)
            return
        # Walk attributes in source order, not _collect_attrs: a virtual machine or real pool
        # repeats `machine:` and its per-member factor and max_load (DL-49). Each `machine:`
        # line opens a component; factor and max_load bind to the latest component, or are
        # machine-level before any opens
        machine_type: str | None = None
        members: list[MachineMember] = []
        level_attrs: dict[str, str] = {}
        current: MachineMember | None = None
        ok = True
        for attr in stmt.attrs:
            key = attr.key.lower()
            value = attr.raw_value.strip()
            if key == "type":
                if machine_type is not None:
                    self.err("duplicate attribute 'type' in one insert_machine", attr.span)
                    ok = False
                    continue
                machine_type = unquote_jil_value(attr.raw_value)
            elif key == "machine":
                opened = len(members)
                for member in (m.strip() for m in value.split(",")):
                    if member:
                        members.append(MachineMember(name=member))
                if len(members) == opened:  # empty `machine:` line: no member, no silent loss
                    self.err("empty 'machine:' pool-member line", attr.span)
                    ok = False
                    continue
                current = members[-1]
            elif key in self._MACHINE_MEMBER_ATTRS and current is not None:
                if key in current.attrs:
                    self.err(f"duplicate {attr.key!r} for pool member {current.name!r}", attr.span)
                    ok = False
                    continue
                current.attrs[key] = value
            else:
                # key stored lowercased, as in _collect_attrs, so the duplicate guard is
                # case-insensitive and the resolver's node_name lookup finds any spelling
                if not self._fresh_attr(level_attrs, attr):
                    ok = False
                    continue
                level_attrs[key] = value
        if not ok:
            return
        self.machines[name] = MachineIR(
            name=name,
            machine_type=machine_type,
            members=members,
            attrs=level_attrs,
            span=stmt.span,
        )

    def _lower_resource(self, stmt: JilStatement) -> None:
        name = self._subject(stmt, "resource")
        attrs = self._collect_attrs(stmt)
        if name is None or attrs is None:
            return
        if name in self.resources:
            self.err(f"duplicate insert_resource {name!r}", stmt.span)
            return
        res_type = None
        if (attr := attrs.pop("res_type", None)) is not None:
            res_type = unquote_jil_value(attr.raw_value)
        self.resources[name] = ResourceIR(
            name=name,
            res_type=res_type,
            attrs={a.key: a.raw_value.strip() for a in attrs.values()},
            span=stmt.span,
        )

    def _lower_xinst(self, stmt: JilStatement) -> None:
        name = self._subject(stmt, "external instance")
        attrs = self._collect_attrs(stmt)
        if name is None or attrs is None:
            return
        if name in self.external_instances:
            self.err(f"duplicate insert_xinst {name!r}", stmt.span)
            return
        xtype = attrs.pop("xtype", None)
        if xtype is None:
            self.err(f"insert_xinst {name!r}: missing xtype attribute (SEM-07)", stmt.span)
            return
        # xmachine (required), xport, xmanager, xcrypt_type and xkey_to_manager are connection
        # plumbing, carried verbatim (DL-28); the engine enforces required-ness
        self.external_instances[name] = XinstIR(
            name=name,
            xtype=unquote_jil_value(xtype.raw_value),
            attrs={a.key: a.raw_value.strip() for a in attrs.values()},
            span=stmt.span,
        )

    def _lower_calendar(self, stmt: JilStatement, *, kind: Literal["standard", "extended"]) -> None:
        name = self._subject(stmt, "calendar")
        if name is None:
            return
        # `condition:` is the one repeatable calendar key, on extended calendars only (SEM-36);
        # every other key keeps the duplicate-is-error rule (_collect_attrs)
        attrs: dict[str, str] = {}
        conditions: list[str] = []
        ok = True
        for attr in stmt.attrs:
            key = attr.key.lower()
            if key == "condition" and kind == "extended":
                conditions.append(attr.raw_value.strip())
            elif not self._fresh_attr(attrs, attr):
                ok = False
            else:
                attrs[key] = attr.raw_value.strip()
        if not ok:
            return
        # unquoted so it compares equal to unquoted run_calendar refs (L018)
        name = unquote_jil_value(name)
        if name in self.calendars:
            self.err(
                f"duplicate calendar {name!r} (standard and extended calendars share the"
                " run_calendar namespace)",
                stmt.span,
            )
            return
        self.calendars[name] = CalendarIR(
            name=name,
            kind=kind,
            attrs=attrs,
            conditions=conditions,
            dates=[row.strip() for row in stmt.date_lines],
            span=stmt.span,
        )

    def _lower_cycle(self, stmt: JilStatement) -> None:
        name = self._subject(stmt, "cycle")
        if name is None:
            return
        # start_date and end_date alternate into periods (SEM-39), paired by position
        attrs: dict[str, str] = {}
        periods: list[tuple[str, str]] = []
        open_start: str | None = None
        ok = True
        for attr in stmt.attrs:
            key = attr.key.lower()
            if key == "start_date":
                if open_start is not None:
                    self.err("start_date without a closing end_date", attr.span)
                    ok = False
                open_start = attr.raw_value.strip()
            elif key == "end_date":
                if open_start is None:
                    self.err("end_date without a preceding start_date", attr.span)
                    ok = False
                else:
                    periods.append((open_start, attr.raw_value.strip()))
                    open_start = None
            elif not self._fresh_attr(attrs, attr):
                ok = False
            else:
                attrs[key] = attr.raw_value.strip()
        if open_start is not None:
            self.err("start_date without a closing end_date", stmt.span)
            ok = False
        if not ok:
            return
        name = unquote_jil_value(name)
        if name in self.cycles:
            self.err(f"duplicate cycle {name!r}", stmt.span)
            return
        self.cycles[name] = CycleIR(name=name, attrs=attrs, periods=periods, span=stmt.span)


def lower_catalog(files: Iterable[JilFile], *, permit_unknown: bool = False) -> CatalogIR:
    """Lower parsed JIL files into one CatalogIR, or raise LoweringError with every finding.

    No partial catalog is returned (DL-04).
    """
    return _Lowerer(permit_unknown).run(files)


def lower_source(text: str, *, file: str = "<memory>", permit_unknown: bool = False) -> CatalogIR:
    """Parse + lower a single JIL text (test/tooling convenience)."""
    return lower_catalog([parse_jil(text, file=file)], permit_unknown=permit_unknown)
