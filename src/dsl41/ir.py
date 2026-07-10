"""IR-F: faithful semantic layer -- entity models + AST -> IR-F lowering.

Phase 3 of the implementation order (CLAUDE.md / DL-03). Normative spec:
docs/ir-design.md ss3-4 (models are the API; lowering rules of note) and
docs/autosys-semantics.md (SEM entries cited per field). Condition algebra
models live in conditions.py (ss3); this module supplies the entity models
(ss4), the lowering with the DL-07 passthrough firewall, and deterministic
serialization (ss8).

Loss policy (ir-design ss1): lowering may normalize syntax (abbreviations,
quoting, list formats) but never semantics; an unknown attribute not on the
inert allow-list is a lowering error unless `permit_unknown` is set (DL-07).

Decisions pinned here (each with a test; spec-sketch gaps resolved, not
relitigated -- see the docstrings of the individual models/handlers):
- SEM-30: time attributes with falsy/absent `date_conditions` are dead config;
  they are NOT modeled as schedule -- they go to `passthrough` verbatim so the
  L005 linter can see them and nothing is lost.
- SEM-34: must_*_times count must match start_times, EXCEPT a single relative
  offset which broadcasts to every start time. [?] The dossier says strict
  count-match, but its own doc-derived corpus fixture (sem30_schedule.jil)
  uses one `+3` against three start_times -- TechDocs' own example. Pin the
  exact rule on a live instance; broadcast is the reading that accepts the
  documented example.
- Subcommand support v1: insert_job / insert_global / insert_machine /
  insert_xinst / insert_resource (DL-18). update_/delete_/override_ forms are
  loud lowering errors -- merging update semantics is real semantics, not
  syntax, and guessing it would be silent loss.
- SEM-24 [A] (DL-18): `status:` on insert_job lowers to
  Semantics.initial_status, restricted to INACTIVE/ON_HOLD/ON_ICE/ON_NOEXEC;
  run states would interact with the SEM-01 latch and are refused loudly.
- DL-21: the `resources:` job attribute (11.3+ resource objects, TechDocs
  12.x) lowers to typed JobIR.resources (name/QUANTITY/FREE per group, AND
  separator); malformed groups are loud errors. QUANTITY is required (every
  documented and estate example carries it); FREE absent stays None (the
  engine default is not guessed). No oracle gate semantics v1.
- job_type is required (no defaulting to CMD): autorep -q output always emits
  it; a missing one in hand-written JIL is more likely an error than an
  intentional default. [?] Relax if a real-estate fixture shape needs it.
- Type-inapplicable exec attributes: command on BOX/FW, watch_* on CMD/BOX,
  and std_in_file/envvars on FW are lowering errors (control-flow-shaped
  attrs on the wrong type = estate smell); machine/owner/profile/std_*/
  envvars on a BOX are inert (boxes do not execute, SEM-10) and go to
  passthrough verbatim.
- Duplicate attribute keys within a statement and duplicate job names within
  a catalog are lowering errors (real autorep output produces neither; last-
  wins would be silent loss). L014 (linter) additionally covers UC-side name
  collision rules.
- Semantic unquoting (statement-syntax rule 7) applies to the TYPED lane only
  (command/exec strings, box_name, calendars, timezone, global values, machine
  type, xtype): exactly one wrapping quote pair, no interior quotes. The
  annotations/passthrough dicts are verbatim lanes per the ss4 sketch, and
  MachineIR.attrs / ResourceIR.attrs are the documented opaque-v1 stance for
  resource/placement records (dossier ss5, DL-18) -- the DL-07 firewall
  guards job semantics, not machine/resource records.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Sequence
from importlib import metadata
from typing import Annotated, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from dsl41.ast_jil import JilFile, JilStatement, RawAttr, SourceSpan
from dsl41.ast_jil import parse as parse_jil
from dsl41.conditions import Cond, ConditionParseError, parse_condition

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
        # 12.x sweep (DL-32): MISSING_HEARTBEAT alarm + the notification-
        # services family -- alarms/emails only, no control flow.
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
#: auto_delete: definition lifecycle. job_load/priority/machine_method:
#: resource/placement (M34). ulimit/elevated/interactive/job_class: OS/agent-
#: side exec tuning. avg_runtime: statistics seed. chk_files: pre-start
#: disk-space gate -- teeth, but Resource-Wait class (dossier ss5 row), no
#: oracle semantics v1. All added by the 12.x doc sweep (DL-32).
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

#: SEM-08: `$$NAME` / `$${NAME}` are AutoSys globals; single-`$` is shell and
#: must stay distinct (never matched here).
_VAR_RE = re.compile(r"\$\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

IR_VERSION: Literal["0.1"] = "0.1"


# ---------------------------------------------------------------- entity models (ss4)


class Time(BaseModel):
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)

    @classmethod
    def parse(cls, text: str) -> Time:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
        if m is None:
            raise ValueError(f"invalid time {text!r} (expected HH:MM)")
        return cls(hour=int(m.group(1)), minute=int(m.group(2)))


class SlaSpec(BaseModel):
    """SEM-34: must_start/must_complete are alarms only -- annotation class,
    no control flow. Absolute times or relative '+n' offsets, never mixed."""

    model_config = ConfigDict(validate_assignment=True)

    kind: Literal["absolute", "relative"]
    times: list[Time] | None = None  # kind == absolute
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


# PENDING: Q7 -- the docs give formats and single-attribute defaults but not
# the composition; the corners are pinned to the conservative direction until
# a live instance settles them (dossier ss9, DL-33).
def exit_is_success(
    exit_code: int,
    *,
    max_exit_success: int = 0,
    success_codes: Sequence[tuple[int, int]] | None = None,
    fail_codes: Sequence[tuple[int, int]] | None = None,
) -> bool:
    """SEM-09 (amended 2026-07-10, DL-33): the SUCCESS/FAILURE verdict for a
    completion exit code -- the single source shared by the oracle and the
    UC twin (M31: same boundary on both sides).

    Documented parts (TechDocs 12.x/24.x): fail_codes names explicit
    failure codes; success_codes, when present, REPLACES the default
    success rule (its absence-default is "0 is success"); otherwise the
    max_exit_success threshold decides (the original SEM-09).

    Q7 corners (PENDING, see the module-level marker above), pinned to the
    conservative direction (never invent a SUCCESS the engine might not
    record): fail_codes wins over success_codes; under a present
    success_codes an unmatched code is FAILURE and the threshold is
    ignored; fail_codes alone falls through to the threshold for
    unmatched codes.
    """

    def _in(ranges: Sequence[tuple[int, int]]) -> bool:
        return any(lo <= exit_code <= hi for lo, hi in ranges)

    if fail_codes and _in(fail_codes):
        return False
    if success_codes is not None:
        return _in(success_codes)
    return exit_code <= max_exit_success


class Semantics(BaseModel):
    """Attributes with control-flow teeth (dossier ss5)."""

    condition: Cond | None = None
    max_exit_success: int = 0  # SEM-09
    success_codes: list[tuple[int, int]] | None = None  # SEM-09/DL-33; CMD-only
    fail_codes: list[tuple[int, int]] | None = None  # SEM-09/DL-33; CMD-only
    term_run_time_min: int | None = None
    n_retrys: int = 0
    box_success: Cond | None = None  # box jobs only; SEM-12
    box_failure: Cond | None = None
    auto_hold: bool = False
    initial_status: InitialStatus | None = None  # SEM-24 [A]: `status:` on insert
    # Provenance (ir-design ss4: every Cond keeps a pointer to its AST span).
    # Cond-node CondSpans are char offsets into the parsed text, which is
    # raw_value.strip(); the scanner never leaves leading whitespace in
    # raw_value, so offsets align with it. These SourceSpans locate the
    # attr in the source file.
    condition_span: SourceSpan | None = None
    box_success_span: SourceSpan | None = None
    box_failure_span: SourceSpan | None = None

    def exit_is_success(self, exit_code: int) -> bool:
        """SEM-09 verdict with this job's configured boundary (DL-33)."""
        return exit_is_success(
            exit_code,
            max_exit_success=self.max_exit_success,
            success_codes=self.success_codes,
            fail_codes=self.fail_codes,
        )


#: The three condition-bearing Semantics attrs, each paired with a
#: `{attr_name}_span` field; shared by JobIR.iter_conditions below.
_CONDITION_ATTRS: tuple[Literal["condition", "box_success", "box_failure"], ...] = (
    "condition",
    "box_success",
    "box_failure",
)


class ResourceRef(BaseModel):
    """One group of the `resources:` job attribute (DL-21; TechDocs 12.x:
    `(name, QUANTITY=n[, FREE=Y|N|A]) AND (...)`). Typed carry, no oracle
    gate semantics v1 (Resource Wait / QUE_WAIT is out of interpreter
    scope); UCS-09 maps these to UC Virtual Resource requirements."""

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
        """(attr_name, cond, attr_span) for every condition-bearing attr set on
        this job (condition/box_success/box_failure). Canonical walker shared
        by lint's L001-family rules and derive's pass-1 extraction -- both
        used to carry their own copy of this loop."""
        for attr_name in _CONDITION_ATTRS:
            cond = getattr(self.sem, attr_name)
            if cond is not None:
                yield attr_name, cond, getattr(self.sem, f"{attr_name}_span")


class MachineIR(BaseModel):
    """insert_machine record; resource/placement semantics are opaque v1
    (dossier ss5), so everything beyond the name/type is carried verbatim."""

    name: str
    machine_type: str | None = None  # JIL `type:` -- r(eal)/v(irtual)/a(gent), verbatim
    attrs: dict[str, str] = {}
    span: SourceSpan | None = None


class ResourceIR(BaseModel):
    """insert_resource record (virtual resources); carried opaquely v1 like
    MachineIR (DL-18) -- UCS-09/M34 map these to UC Virtual Resources when the
    backend lands, and typing amount/res_type before a consumer exists would
    be speculation."""

    name: str
    res_type: str | None = None  # JIL `res_type:` -- verbatim
    attrs: dict[str, str] = {}
    span: SourceSpan | None = None


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


class CatalogMeta(BaseModel):
    source_files: list[str] = []
    tool_version: str = ""
    parsed_at: str | None = None  # caller-stamped; None keeps dumps deterministic


_BOX_DEPTH_SANITY = 64  # ir-design ss4 "<= depth sanity"; SEM-17: nesting is legal


def _box_tree_problems(jobs: dict[str, JobIR]) -> list[tuple[str, str]]:
    """(member_name, message) pairs; shared by the CatalogIR validator (ground
    truth on every construction) and lowering (which adds source spans).
    Membership problems and cycles are reported together (LoweringError
    carries every finding); a chase link already reported as missing/non-box
    is skipped rather than re-reported."""
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

    ir_version: Literal["0.1"] = IR_VERSION
    jobs: dict[str, JobIR] = {}
    globals_declared: dict[str, str] = {}  # insert_global
    external_instances: dict[str, XinstIR] = {}  # insert_xinst (SEM-07), plumbing opaque (DL-28)
    machines: dict[str, MachineIR] = {}
    resources: dict[str, ResourceIR] = {}  # insert_resource, opaque v1 (DL-18)
    meta: CatalogMeta = CatalogMeta()

    @model_validator(mode="after")
    def _box_tree_is_sound(self) -> CatalogIR:
        problems = _box_tree_problems(self.jobs)
        if problems:
            raise ValueError("; ".join(msg for _, msg in problems))
        return self


# ---------------------------------------------------------------- serialization (ss8)


def dump_catalog(catalog: CatalogIR) -> str:
    """Deterministic JSON: sorted keys, stable indent, trailing newline --
    one catalog = one file, diff-able in git (ir-design ss8)."""
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
        return f"{self.span.file}:{self.span.line_start}: {self.message}"


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
}

#: Exec-cluster attributes valid on both CMD and FW jobs (ExecSpecBase).
_EXEC_BASE_ATTRS = ("machine", "owner", "profile", "std_out_file", "std_err_file")

#: Exec-shaped attributes that are inert on a BOX (boxes do not execute,
#: SEM-10) and route to passthrough verbatim; the CMD-only pair joins the
#: base cluster here (DL-32).
_BOX_INERT_ATTRS = frozenset(_EXEC_BASE_ATTRS) | {"std_in_file", "envvars"}


_WRAPPED_QUOTES_RE = re.compile(r'"[^"]*"')


def _unquote(value: str) -> str:
    """Semantic unquoting (jil-statement-syntax rule 7) for the typed lane:
    exactly one wrapping quote pair with no interior quotes. Anything else --
    partial quoting, multiple pairs -- belongs to the value (e.g. shell syntax
    inside command) and is returned stripped-but-verbatim."""
    v = value.strip()
    if _WRAPPED_QUOTES_RE.fullmatch(v):
        return v[1:-1]
    return v


def _split_list(value: str) -> list[str]:
    """Comma-separated JIL list; tolerates newlines (rule-6 continuations) and
    empty segments (trailing commas) -- lexical normalization only."""
    return [t for t in (part.strip() for part in _unquote(value).split(",")) if t]


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
        self.source_files: list[str] = []

    def err(self, message: str, span: SourceSpan | None) -> None:
        self.findings.append(LoweringFinding(message=message, span=span))

    # ------------------------------------------------------------------ driver

    def run(self, files: Iterable[JilFile]) -> CatalogIR:
        for jf in files:
            self.source_files.append(jf.file)
            for stmt in jf.statements:
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
            meta=CatalogMeta(source_files=self.source_files, tool_version=tool_version()),
        )

    # ------------------------------------------------------------- shared helpers

    def _collect_attrs(self, stmt: JilStatement) -> dict[str, RawAttr] | None:
        """Key-lowered attr map; duplicate keys are lowering errors (module
        docstring: last-wins would be silent loss)."""
        attrs: dict[str, RawAttr] = {}
        ok = True
        for attr in stmt.attrs:
            key = attr.key.lower()
            if key in attrs:
                self.err(f"duplicate attribute {attr.key!r} in one statement", attr.span)
                ok = False
            else:
                attrs[key] = attr
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
        """SEM-09/DL-33 exit-code sets: a comma list of single codes and
        lo-hi ranges (TechDocs: '4', '0-9999', '1,3,20-30'). Sorted, never
        merged -- the surface partition is the author's, only membership
        is semantics."""
        ranges: list[tuple[int, int]] = []
        for token in _split_list(attr.raw_value):
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

    def _cond_attr(self, attr: RawAttr) -> Cond | None:
        try:
            return parse_condition(attr.raw_value.strip())
        except ConditionParseError as exc:
            self.err(f"{attr.key}: {exc}", attr.span)
            return None

    # ------------------------------------------------------------------ insert_job

    _RESOURCE_GROUP_RE = re.compile(r"\(([^()]*)\)")

    def _parse_resources(self, attr: RawAttr) -> list[ResourceRef]:
        """`(name, QUANTITY=n[, FREE=Y|N|A]) AND (...)` (DL-21; TechDocs 12.x
        keywords, case-insensitive; estates also write lowercase `and`).
        Anything else is a loud lowering error -- guessing a resource gate
        would be silent loss."""

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
                # Broadcom's and the estate's examples always carry QUANTITY;
                # defaulting would be a guess -- extend deliberately if a bare
                # group ever shows up in a real export.
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
        if name is not None and name in self.jobs:
            self.err(f"duplicate job name {name!r} in compilation set", stmt.span)
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
            # SEM-30: time attrs without truthy date_conditions are dead config;
            # carried verbatim (incl. the falsy switch itself) for L005
            # visibility, no schedule semantics.
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
            box_name = _unquote(attr.raw_value)
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
            sem.condition_span = attr.span
        if (attr := attrs.pop("box_success", None)) is not None:
            sem.box_success = self._cond_attr(attr)
            sem.box_success_span = attr.span
        if (attr := attrs.pop("box_failure", None)) is not None:
            sem.box_failure = self._cond_attr(attr)
            sem.box_failure_span = attr.span
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
            value = _unquote(attr.raw_value).upper()
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
            attrs.pop("date_conditions")  # finding recorded; keep it out of passthrough
            return None
        if not truthy:
            return None  # falsy stays in attrs -> passthrough (SEM-30 dead config)
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
            fields["run_calendar"] = _unquote(attr.raw_value)
        if (attr := take("exclude_calendar")) is not None:
            fields["exclude_calendar"] = _unquote(attr.raw_value)
        if (attr := take("timezone")) is not None:
            fields["timezone"] = _unquote(attr.raw_value)
        if (attr := take("start_times")) is not None:
            fields["start_times"] = self._times_attr(attr)
        if (attr := take("start_mins")) is not None:
            fields["start_mins"] = self._ints_attr(attr)
        if (attr := take("run_window")) is not None:
            fields["run_window"] = self._run_window(attr)
        start_times = fields.get("start_times")
        n_starts = len(start_times) if isinstance(start_times, list) else None
        if (attr := take("must_start_times")) is not None:
            fields["must_start"] = self._sla_attr(attr, n_starts)
        if (attr := take("must_complete_times")) is not None:
            fields["must_complete"] = self._sla_attr(attr, n_starts)
        # SEM-31 pre-check so findings point at the conflicting attribute line
        # (presence-based; the model validator below stays the ground truth).
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
        parts = _unquote(attr.raw_value).split("-")
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

    def _sla_attr(self, attr: RawAttr, n_start_times: int | None) -> SlaSpec | None:
        """SEM-34 must_*_times: absolute or relative, never mixed; count must
        match start_times, except a single relative offset broadcasts (module
        docstring, [?] pin on live instance)."""
        tokens = _split_list(attr.raw_value)
        if not tokens:
            self.err(f"{attr.key}: empty value", attr.span)
            return None
        if n_start_times is None:
            self.err(f"{attr.key}: requires start_times (SEM-34)", attr.span)
            return None
        relative = [t.startswith("+") for t in tokens]
        if any(relative) and not all(relative):
            self.err(f"{attr.key}: absolute and relative forms cannot be mixed (SEM-34)", attr.span)
            return None
        if all(relative):
            offsets: list[int] = []
            for t in tokens:
                try:
                    offsets.append(int(t[1:]))
                except ValueError:
                    self.err(f"{attr.key}: invalid relative offset {t!r}", attr.span)
                    return None
            if len(offsets) not in (1, n_start_times):
                self.err(
                    f"{attr.key}: {len(offsets)} offsets for {n_start_times} start_times"
                    " (SEM-34: count must match; a single offset broadcasts)",
                    attr.span,
                )
                return None
            return SlaSpec(kind="relative", offsets_min=offsets)
        times = []
        for t in tokens:
            try:
                times.append(Time.parse(t))
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
        return SlaSpec(kind="absolute", times=times)

    # ----------------------------------------------------------------- exec specs

    def _exec_spec(self, job_type: str, attrs: dict[str, RawAttr]) -> ExecSpec | FwSpec | None:
        command = attrs.pop("command", None)
        watch_file = attrs.pop("watch_file", None)
        watch_interval = attrs.pop("watch_interval", None)
        watch_min_size = attrs.pop("watch_file_min_size", None)
        if job_type == "BOX":
            # Boxes do not execute (SEM-10): command/watch_* are control-flow-
            # shaped on the wrong type -> error. The base cluster (machine,
            # owner, ...) is inert on a box: it stays in `attrs` and the
            # classifier routes it to passthrough with its real spans.
            for wrong in (command, watch_file, watch_interval, watch_min_size):
                if wrong is not None:
                    self.err(f"{wrong.key}: not valid on a BOX job", wrong.span)
            return None
        base: dict[str, str] = {}
        for field_name in _EXEC_BASE_ATTRS:
            if (attr := attrs.pop(field_name, None)) is not None:
                base[field_name] = _unquote(attr.raw_value)
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
                    "command": _unquote(command.raw_value),
                    "std_in_file": _unquote(std_in.raw_value) if std_in is not None else None,
                    "envvars": _unquote(envvars.raw_value) if envvars is not None else None,
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
                "watch_file": _unquote(watch_file.raw_value),
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
        self.globals_declared[name] = _unquote(value.raw_value)

    def _lower_machine(self, stmt: JilStatement) -> None:
        name = self._subject(stmt, "machine")
        attrs = self._collect_attrs(stmt)
        if name is None or attrs is None:
            return
        if name in self.machines:
            self.err(f"duplicate insert_machine {name!r}", stmt.span)
            return
        machine_type = None
        if (attr := attrs.pop("type", None)) is not None:
            machine_type = _unquote(attr.raw_value)
        self.machines[name] = MachineIR(
            name=name,
            machine_type=machine_type,
            attrs={a.key: a.raw_value.strip() for a in attrs.values()},
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
            res_type = _unquote(attr.raw_value)
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
        # TechDocs 12.x also documents xmachine (required), xport, xmanager,
        # xcrypt_type, xkey_to_manager -- connection plumbing, carried
        # verbatim (DL-28); required-ness is the engine's to enforce, not
        # this per-file-tolerant lowering's.
        self.external_instances[name] = XinstIR(
            name=name,
            xtype=_unquote(xtype.raw_value),
            attrs={a.key: a.raw_value.strip() for a in attrs.values()},
            span=stmt.span,
        )


def lower_catalog(files: Iterable[JilFile], *, permit_unknown: bool = False) -> CatalogIR:
    """Lower parsed JIL files into one CatalogIR; raises LoweringError with
    every finding (never a partial catalog -- no silent loss, DL-04)."""
    return _Lowerer(permit_unknown).run(files)


def lower_source(text: str, *, file: str = "<memory>", permit_unknown: bool = False) -> CatalogIR:
    """Parse + lower a single JIL text (test/tooling convenience)."""
    return lower_catalog([parse_jil(text, file=file)], permit_unknown=permit_unknown)
