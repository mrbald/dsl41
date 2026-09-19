"""Estate minifier: a de-identified JIL estate that still exercises the compiler.

The job this module exists for: an owner holds a real AutoSys estate and wants
to hand over a test case. What may leave is the STRUCTURE dsl41 models -- job
graph, conditions, schedules, exit-code policy, resource gates. What may not
leave is anything naming the estate: job names, hosts, commands, paths, owners,
descriptions, comments. One leaked identifier is the failure that matters, so
every rule here fails CLOSED in both directions.

Layering (deliberate, not incidental): parse with `ast_jil.parse`, transform the
statement AST, render with `render_preserve` over an AST whose trivia has been
EMPTIED -- comments, blank lines, indents and separator runs are free text, so
the transform clears them rather than trusting the renderer to.

Preserve, not canonical. Canonical mode sorts a statement's attributes into a
fixed key order, which is lossless for every statement whose lowering reads
attributes as a map and lossy for the two kinds that read them in SOURCE order:
an insert_machine pool binds `factor`/`max_load` to the `machine:` line above
them (DL-49), and a cycle pairs `start_date`/`end_date` positionally (SEM-39).
Preserve mode keeps `stmt.attrs` in source order, so both survive with no
refusal and no change to the scanner spec.

The price of preserve mode is that trivia is reproduced byte for byte, which
makes one channel load-bearing: a `#` tail is VALUE text, not a comment (DL-31
-- `#` is a legal name character and the vendor defines `#` comments in column
one only), so the scanner leaves it INSIDE `stmt.subject` and creates no
`Comment` object for it. It vanishes only because every subject is replaced by
a minted name, so `_transform_statement` ASSERTS that -- an unminted subject
reaching the renderer is a refusal. The token guard is not trusted for it.

Classification is the core rule. Every attribute key, and every statement
subcommand, resolves to exactly one class:

  KEEP     the value goes out verbatim, AFTER `validate_keep` proves it lies
           in the closed space its key claims -- a vendor enum, a number, a
           clock time, a day token, an IANA zone, a date, a SEM-37 calendar
           keyword. None of those can name an estate. The check is the class:
           lowering polices only about half these keys and carries the rest
           opaquely, so "KEEP" meaning "assumed to be in that space" shipped a
           client string written into `timezone` or `job_load` verbatim. A
           value outside the space is a refusal, not a passthrough.
  RENAME   the value is an identifier; it is mapped into a synthetic namespace.
  REPLACE  the value is free text whose PRESENCE is semantic; it is swapped for
           a fixed inert constant. `command` is the only member: lowering
           refuses a CMD job without one (so it cannot be dropped) and its text
           is the single richest source of client identity in an estate.
           Nothing else qualifies -- every other free-text attribute either
           carries an identifier (RENAME) or has no dsl41 semantics (DROP).
  DROP     the attribute is not emitted. Reserved for attributes dsl41 does not
           model beyond carrying them verbatim, whose values are free text or
           site-chosen names.

A key that matches none of them is a REFUSAL, listing every such key with
file:line and changing nothing. An unknown key must neither leak (KEEP by
default) nor silently vanish (DROP by default); DL-07 already takes that stance
at lowering and this module takes it one layer earlier.

The table is derived from what the IR actually models: `ANNOTATION_ATTRS`,
`PASSTHROUGH_ALLOWED`, `TIME_CLUSTER`, `EXEC_BASE_ATTRS` and `_BOX_INERT_ATTRS`
in `ir.py`, plus the keys `_Lowerer` pops by name on each statement kind. Where
the IR carries an attribute opaquely AND its value is a site-chosen string
(`job_class`, `machine_method`, `application`, `group`, `permission`,
`chk_files`, the xinst connection plumbing), this module drops it: an opaque
carry is not a semantic the test case needs, and the string may name the estate.

Naming is positional and deterministic: boxes `b<N>`, a member of box `b<N>`
`b<N>j<M>`, an unboxed job `j<N>`, machines `m<N>`, resources and locks `l<N>`,
calendars and cycles `c<N>`, globals `g<N>`, external instances `x<N>`,
non-numeric global values `v<N>`, watched files `/f/<N>`. `<N>` is base36
lowercase from 1, allocated at an identifier's FIRST appearance anywhere in the
input -- a reference counts, so an estate that names a box before defining it
still numbers deterministically. Box membership is resolved in a pre-pass, so a
job that moves box across statements gets one stable name.

Conditions are rewritten through `conditions.parse_condition` + `iter_atoms`:
only the identifier text inside each atom's `CondSpan` changes, right-to-left so
earlier spans stay valid. Operators, whitespace, parentheses and lookback tokens
(SEM-04, `s(X, 0)` included) come out byte-identical. A condition that will not
parse is a refusal, never a passthrough.

A RENAME value is rewritten WHOLE, never substituted into. `resources:` used to
be patched by regex, which left the text outside the parentheses, the tail of a
group after its first comma, and a paren-less value untouched -- free estate
text behind nothing but the guard's floor. Every RENAME lane now parses its
value and re-emits it from the parsed parts, and anything the grammar does not
cover is a refusal.

Two checks stand behind the transform:

  Structural verify (`--no-verify` skips it) lowers both estates and asserts
  they are isomorphic under the mapping -- job set, job types, box membership,
  condition atom structure, resource demands, mutex groups (M07), and the
  SEM-30 time cluster. This is what proves the artifact still exercises what it
  is supposed to.

  The leak guard is not skippable. Every identifier, path and free-text value in
  the input becomes a needle; if any appears in the rendered output the run
  refuses. Three things account for an output token and so are not needles: the
  JIL/dsl41 vocabulary, the synthetic names this run allocated, and the emitted
  KEEP values -- the last only because `validate_keep` proved each of them
  closed-space first. Its honest limits: a needle token must be at least 4
  characters and carry a letter, because shorter and all-digit tokens collide
  with kept clock times and timing hints and a guard that fires on every estate
  gets switched off; and a client name that happens to BE a legal closed-space
  value is invisible to it. The classification table plus its predicates are
  the defence there, not this guard -- which is why `_v_timezone` enumerates
  instead of asking a resolver whether a string can be interpreted. The guard
  is a backstop, never a total check, and no user-facing text may call it one.

The rendered bytes are re-scanned and checked to say what the transform meant:
the same statements, keys and values, and no comment anywhere. That catches a
rewritten value that would re-read as something else -- a rule-5 comment opener
above all.

Rule-11 calendar date rows are validated too, by `autocal.standard_rows` -- the
scanner carries ANY non-attribute line under a `calendar:` statement verbatim,
so an export's label column would otherwise ship unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from dsl41.ast_jil import (
    Comment,
    JilFile,
    JilParseError,
    JilStatement,
    RawAttr,
    render_preserve,
)
from dsl41.ast_jil import parse as parse_jil
from dsl41.conditions import (
    And,
    Cond,
    ConditionParseError,
    ExitCodeAtom,
    GlobalAtom,
    Or,
    Paren,
    StatusAtom,
    escape_job_name,
    iter_atoms,
    parse_condition,
    unescape_job_name,
)
from dsl41.ir import (
    CatalogIR,
    FwSpec,
    JobIR,
    LoweringError,
    lower_catalog,
    unquote_jil_value,
)
from dsl41.minify_rules import (
    BOX_JOB_TYPES,
    JOB_SUBCOMMANDS,
    SUBJECT_NAMESPACE,
    Klass,
    classify,
    validate_date_rows,
    validate_keep,
    vocabulary_words,
)

#: The mechanism half's own surface. The policy names this module imports are
#: NOT re-exported: the policy/mechanism cut is the point of the split
#: (DL-207), and a second import path to `classify` or `validate_keep` loses it
#: (DL-75 review 2026-09-19). Policy is imported from `minify_rules`.
__all__ = [
    "MinifyRefusal",
    "MinifyResult",
    "NameMap",
    "leak_findings",
    "minify_files",
    "output_paths",
    "verify_findings",
]

_B36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


class MinifyRefusal(Exception):
    """A loud refusal carrying every reason at once (never a partial answer).

    The CLI prints `messages` one per line and exits 3. Collecting all of them
    matters most for the unclassified-key case: an owner extending the table
    wants the whole list, not the first key.
    """

    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


class ValueRewriteError(ValueError):
    """A RENAME value that does not parse, so minify cannot rewrite all of it.

    Partial rewriting is the defect this exists to prevent: a `resources:`
    value whose tail minify did not understand used to ship verbatim behind
    nothing but the guard's 4-character floor.
    """


# ------------------------------------------------------------ the mapping


def _b36(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n, 36)
        out = _B36_DIGITS[rem] + out
    return out or "0"


@dataclass
class _Series:
    """One synthetic namespace: a template and a monotone base36 counter.

    `taken` is shared across every series that mints a JOB name, because the
    shapes alias: a box at index 1981 spells `b1j1`, which is also the first
    member of box `b1` (base36 digits include `j`). Skipping an already-minted
    name keeps the scheme and closes the alias.
    """

    template: str
    taken: set[str] = field(default_factory=set)
    next_index: int = 1

    def take(self) -> str:
        while True:
            name = self.template.replace("#", _b36(self.next_index))
            self.next_index += 1
            if name not in self.taken:
                self.taken.add(name)
                return name


@dataclass
class NameMap:
    """old -> new for every namespace. Serialized by `--mapping`.

    Re-identifies the estate on its own; it is never written unless asked for
    and never printed to stdout.
    """

    jobs: dict[str, str] = field(default_factory=dict)
    machines: dict[str, str] = field(default_factory=dict)
    resources: dict[str, str] = field(default_factory=dict)
    calendars: dict[str, str] = field(default_factory=dict)
    globals_: dict[str, str] = field(default_factory=dict)
    instances: dict[str, str] = field(default_factory=dict)
    watch_files: dict[str, str] = field(default_factory=dict)
    global_values: dict[str, str] = field(default_factory=dict)
    #: SEM-35 zones. Empty unless --scrub-timezones asked for it, in which case
    #: every zone maps to UTC; verify reads it so the scrub stays isomorphic.
    timezones: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "jobs": self.jobs,
                    "machines": self.machines,
                    "resources": self.resources,
                    "calendars": self.calendars,
                    "globals": self.globals_,
                    "instances": self.instances,
                    "watch_files": self.watch_files,
                    "global_values": self.global_values,
                    "timezones": self.timezones,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


class _Allocator:
    """Allocates a synthetic name at an identifier's first appearance.

    Box membership comes from a pre-pass because it decides the SHAPE of a job
    name (`b1j2` vs `j7`) and a job may state its box in a later statement than
    the one that first names it.
    """

    def __init__(
        self, boxes: set[str], job_boxes: dict[str, str], *, scrub_timezones: bool = False
    ) -> None:
        self._boxes = boxes
        self._job_boxes = job_boxes
        self.scrubbing_timezones = scrub_timezones
        self._resolving: set[str] = set()
        self._members: dict[str, _Series] = {}
        self.names = NameMap()
        #: every synthetic name this run has minted, in any namespace. The
        #: leak guard treats them as vocabulary: a token that IS an allocated
        #: name was authored here, not by the estate (an estate job literally
        #: named `b1j1` still renames, and its own name stays a needle).
        self.allocated: set[str] = set()
        self._job_names: set[str] = set()
        self._box_series = _Series("b#", self._job_names)
        self._free_series = _Series("j#", self._job_names)
        self._series = {
            "machine": _Series("m#"),
            "resource": _Series("l#"),
            "calendar": _Series("c#"),
            "global": _Series("g#"),
            "instance": _Series("x#"),
            "watch": _Series("/f/#"),
            "gvalue": _Series("v#"),
        }
        self._tables = {
            "machine": self.names.machines,
            "resource": self.names.resources,
            "calendar": self.names.calendars,
            "global": self.names.globals_,
            "instance": self.names.instances,
            "watch": self.names.watch_files,
            "gvalue": self.names.global_values,
        }

    def simple(self, namespace: str, name: str) -> str:
        """Map a name in any namespace except `job` (whose shape is derived)."""
        table = self._tables[namespace]
        existing = table.get(name)
        if existing is not None:
            return existing
        allocated = self._series[namespace].take()
        table[name] = allocated
        self.allocated.add(allocated)
        return allocated

    def timezone(self, zone: str) -> str:
        """SEM-35 zone: itself, or UTC when --scrub-timezones asked.

        A zone name is public vocabulary, not an identifier, so the default
        keeps it -- the estate stops exercising SEM-35 otherwise. It still
        discloses a REGION, which is why the choice is explicit in both
        directions and the CLI names the surviving zones out loud.
        """
        mapped = "UTC" if self.scrubbing_timezones else zone
        self.names.timezones[zone] = mapped
        return mapped

    def gvalue(self, value: str) -> str:
        """A global's value. A purely numeric one maps to ITSELF: a number
        names nobody, and `compare_value` compares two integers numerically --
        mapping `100` to a `v#` token would turn `v(X) > 100` into a string
        comparison and change the semantics the test case exists to exercise.
        """
        if not value.strip() or _is_number(value.strip()):
            return value
        return self.simple("gvalue", value)

    def job(self, name: str) -> str:
        """A job name, in the shape its box membership gives it."""
        existing = self.names.jobs.get(name)
        if existing is not None:
            return existing
        parent = self._job_boxes.get(name)
        if name in self._boxes or parent is None or name in self._resolving:
            # A box is `b#` at every nesting depth; an unboxed job is `j#`. A
            # box cycle (A in B, B in A) would recurse forever, so a name
            # already being resolved falls back to the unboxed shape -- the
            # cycle itself is lowering's refusal to make, in its own words.
            series = self._box_series if name in self._boxes else self._free_series
            allocated = series.take()
            self.names.jobs[name] = allocated
            self.allocated.add(allocated)
            return allocated
        self._resolving.add(name)
        parent_name = self.job(parent)
        self._resolving.discard(name)
        series = self._members.setdefault(parent_name, _Series(f"{parent_name}j#", self._job_names))
        allocated = series.take()
        self.names.jobs[name] = allocated
        self.allocated.add(allocated)
        return allocated


def _is_number(text: str) -> bool:
    try:
        int(text)
    except ValueError:
        return False
    return True


def _box_membership(files: Iterable[JilFile]) -> tuple[set[str], dict[str, str]]:
    """Pre-pass: which names are boxes, and which box each job sits in.

    First statement wins for both. A job that MOVES box across statements
    therefore keeps one stable name; which of the two boxes it is filed under
    is arbitrary but deterministic, and the box linkage itself is carried by
    `box_name` in the output, not by the name.
    """
    boxes: set[str] = set()
    job_boxes: dict[str, str] = {}
    for jf in files:
        for stmt in jf.statements:
            if stmt.subcommand.lower() not in JOB_SUBCOMMANDS:
                continue
            name = unescape_job_name(stmt.subject.strip())
            if not name:
                continue
            if _job_type_of(stmt) in BOX_JOB_TYPES:
                boxes.add(name)
            for attr in stmt.attrs:
                if attr.key.lower() == "box_name" and name not in job_boxes:
                    box = unescape_job_name(unquote_jil_value(attr.raw_value))
                    if box:
                        job_boxes[name] = box
    return boxes, job_boxes


def _job_type_of(stmt: JilStatement) -> str | None:
    """The job_type this statement states, lowercased, inline form included."""
    if stmt.job_type_inline is not None:
        return stmt.job_type_inline.strip().lower()
    for attr in stmt.attrs:
        if attr.key.lower() == "job_type":
            return attr.raw_value.strip().lower()
    return None


# ------------------------------------------------------------ conditions

#: JOB_NAME and GLOBAL_NAME as condition.lark spells them; the instance suffix
#: and the comparand tail likewise. Matching the grammar's own character
#: classes is what keeps the rewrite byte-exact outside the identifier.
_JOB_NAME_RE = re.compile(r"(?:[^\s(),^&|:\\]|\\:)+")
_INSTANCE_RE = re.compile(r"[A-Za-z0-9_#@$]+")
_GLOBAL_NAME_RE = re.compile(r"[^\s(),=<>!&|]+")
_COMPARAND_RE = re.compile(r"\)\s*(?:!=|<=|>=|=|<|>)\s*(\"[^\"]*\"|[^\s()&|]+)")


def _splice(text: str, start: int, end: int, replacement: str) -> str:
    return text[:start] + replacement + text[end:]


def _rewrite_job_atom(body: str, alloc: _Allocator) -> str:
    """Rename the job reference inside one status/exitcode atom's text.

    Names are ALLOCATED left to right (first appearance is what fixes a
    number) and SPLICED right to left (an earlier edit would move a later
    offset). Both halves of the rule, one pass.
    """
    open_paren = body.index("(")
    name_match = _JOB_NAME_RE.search(body, open_paren + 1)
    if name_match is None:
        raise ConditionParseError(f"no job reference inside atom {body!r}", text=body)
    new_name = escape_job_name(alloc.job(unescape_job_name(name_match.group(0))))
    inst_match = None
    if body[name_match.end() : name_match.end() + 1] == "^":
        inst_match = _INSTANCE_RE.match(body, name_match.end() + 1)
    new_instance = alloc.simple("instance", inst_match.group(0)) if inst_match else ""
    if inst_match is not None:
        body = _splice(body, inst_match.start(), inst_match.end(), new_instance)
    return _splice(body, name_match.start(), name_match.end(), new_name)


def _rewrite_global_atom(body: str, alloc: _Allocator) -> str:
    """Rename the global and map its comparand inside one value() atom."""
    open_paren = body.index("(")
    name_match = _GLOBAL_NAME_RE.search(body, open_paren + 1)
    if name_match is None:
        raise ConditionParseError(f"no global name inside atom {body!r}", text=body)
    new_name = alloc.simple("global", name_match.group(0))
    tail = _COMPARAND_RE.search(body, name_match.end())
    if tail is not None:
        raw = tail.group(1)
        quoted = raw.startswith('"') and raw.endswith('"')
        mapped = alloc.gvalue(raw[1:-1] if quoted else raw)
        body = _splice(body, tail.start(1), tail.end(1), f'"{mapped}"' if quoted else mapped)
    return _splice(body, name_match.start(), name_match.end(), new_name)


def rewrite_condition(text: str, alloc: _Allocator) -> str:
    """Rename every identifier in one condition expression, nothing else.

    Atoms are rewritten in source order, so names are numbered by first
    appearance, and the rewritten bodies are spliced back right-to-left, so
    each atom's `CondSpan` offsets are still valid when its turn comes.
    Operators, whitespace, parentheses and SEM-04 lookback tokens are never
    touched -- they are outside every identifier's span.
    """
    cond = parse_condition(text)
    edits: list[tuple[int, int, str]] = []
    for atom in iter_atoms(cond):
        if atom.span is None:
            raise ConditionParseError("condition atom carries no source span", text=text)
        body = text[atom.span.start : atom.span.end]
        if isinstance(atom, GlobalAtom):
            body = _rewrite_global_atom(body, alloc)
        elif isinstance(atom, (StatusAtom, ExitCodeAtom)):
            body = _rewrite_job_atom(body, alloc)
        edits.append((atom.span.start, atom.span.end, body))
    for start, end, body in reversed(edits):
        text = _splice(text, start, end, body)
    return text


# ------------------------------------------------------------ value rewrites

_RESOURCE_GROUP_RE = re.compile(r"\(([^()]*)\)")
#: SEM-08 global substitution sites. Same shape as ir._VAR_RE: single-`$` is
#: shell and must stay distinct.
_VAR_SITE_RE = re.compile(r"\$\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_VAR_SITE_ONLY_RE = re.compile(r"\$\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)")

#: A reference list separator: a comma, or the newline a rule-6 continuation
#: folded into the value. `run_calendar`/`exclude_calendar` are continuation
#: attributes, so a multi-line value is one list, not one name.
_REF_SEPARATOR_RE = re.compile(r"[,\n]")


def _rewrite_ref_list(value: str, alloc: _Allocator, namespace: str) -> str:
    """Every member of a reference list renames; separators stay put.

    One rule for four lanes. An insert_machine repeats `machine:` per pool
    member and may list several on one line (DL-49); a job's `machine:` is a
    comma list that L017 splits the same way; `run_calendar`/`exclude_calendar`
    fold their continuation lines into one value (rule 6). Mapping a whole raw
    value as ONE name turned a clean multi-machine estate into an L017-dirty
    one, and left a second calendar name unrenamed behind the guard's floor.
    """
    members = [part.strip() for part in _REF_SEPARATOR_RE.split(unquote_jil_value(value))]
    mapped = [alloc.simple(namespace, name) for name in members if name]
    if not mapped:
        raise ValueRewriteError(f"expected at least one reference, got {value.strip()!r}")
    return ", ".join(mapped)


#: DL-21 group keywords and the closed value space of each.
_RESOURCE_KEYWORDS = ("QUANTITY", "FREE")
_FREE_VALUES = frozenset({"Y", "N", "A"})


def _rewrite_resources(value: str, alloc: _Allocator) -> str:
    """`(name, QUANTITY=n[, FREE=Y|N|A]) AND (...)` (DL-21), REBUILT.

    Substituting into the source text left three things unrewritten: anything
    outside the parentheses, the tail of a group after its first comma, and a
    value with no parentheses at all -- each of them free estate text with only
    the guard's 4-character floor behind it. This parses the whole value and
    emits it from the parsed parts, so every byte out is either a mapped name
    or a fixed keyword. Anything the DL-21 grammar does not cover is a refusal,
    which is what lowering does with it too.
    """
    raw = unquote_jil_value(value).strip()
    groups = _RESOURCE_GROUP_RE.findall(raw)
    separators = [t for t in _RESOURCE_GROUP_RE.sub(" ", raw).split() if t]
    if not groups or len(separators) != len(groups) - 1:
        raise ValueRewriteError(f"expected '(name, ...)' groups joined by AND, got {raw!r}")
    if any(sep.lower() != "and" for sep in separators):
        raise ValueRewriteError(f"resource groups must be joined by AND, got {raw!r}")
    rendered: list[str] = []
    for group in groups:
        parts = [p.strip() for p in group.split(",")]
        name = parts[0]
        if not name or "=" in name:
            raise ValueRewriteError(f"group {group!r} must start with a resource name")
        fields = [alloc.simple("resource", name)]
        for part in parts[1:]:
            fields.append(_resource_field(part, group))
        rendered.append(f"({', '.join(fields)})")
    return " AND ".join(rendered)


def _resource_field(part: str, group: str) -> str:
    """One `KEY=VALUE` inside a resource group, re-emitted from its parts."""
    key, sep, raw_value = part.partition("=")
    name, text = key.strip().upper(), raw_value.strip().upper()
    if not sep or name not in _RESOURCE_KEYWORDS:
        raise ValueRewriteError(f"group {group!r}: expected QUANTITY= or FREE=, got {part!r}")
    if name == "QUANTITY" and not text.isdigit():
        raise ValueRewriteError(f"group {group!r}: QUANTITY expects an integer, got {text!r}")
    if name == "FREE" and text not in _FREE_VALUES:
        raise ValueRewriteError(f"group {group!r}: FREE expects Y, N or A, got {text!r}")
    return f"{name}={text}"


def rewrite_var_sites(value: str, alloc: _Allocator) -> str:
    """`$$NAME` / `$${NAME}` in any emitted value (SEM-08).

    A substitution site is a global REFERENCE wherever it appears, so it renames
    even inside a KEEP value: leaving it would both leak the global's name and
    break the var_sites the IR indexes.
    """

    def one(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        mapped = alloc.simple("global", name)
        return f"$${{{mapped}}}" if match.group(1) else f"$${mapped}"

    return _VAR_SITE_RE.sub(one, value)


def _rewrite_value(rule: str, value: str, alloc: _Allocator) -> str:
    if rule == "cond":
        return rewrite_condition(value.strip(), alloc)
    if rule == "resources":
        return _rewrite_resources(value.strip(), alloc)
    if rule in ("machine", "calendar"):
        return _rewrite_ref_list(value.strip(), alloc, rule)
    bare = unquote_jil_value(value)
    if not bare:
        return value.strip()
    if _VAR_SITE_ONLY_RE.fullmatch(bare):
        # The whole value is one SEM-08 substitution site (a templated estate
        # writes `watch_file: $$INBOX`). Renaming it wholesale would mint a
        # synthetic name for a reference and destroy the var site the IR
        # indexes; renaming the GLOBAL keeps both. A value that only CONTAINS a
        # site (`/data/$$REGION/in`) still renames whole -- it is a path, and
        # the path is the thing that names the estate.
        return rewrite_var_sites(bare, alloc)
    if rule == "job":
        return alloc.job(unescape_job_name(bare))
    if rule == "gvalue":
        return alloc.gvalue(bare)
    return alloc.simple(rule, bare)


# ------------------------------------------------------------ the transform


@dataclass
class _Transformed:
    """One minified file plus the raw material the leak guard needs."""

    jil: JilFile
    needles: set[str]
    kept: set[str]


def _transform_statement(
    stmt: JilStatement, alloc: _Allocator, out: _Transformed, refusals: list[str]
) -> JilStatement | None:
    bad_rows = validate_date_rows(stmt.date_lines)
    if bad_rows is not None:
        refusals.append(
            f"{stmt.span.at}: {bad_rows}; a rule-11 row that is not a date is free text"
            " and minify refuses to ship it"
        )
        return None
    namespace = SUBJECT_NAMESPACE.get(stmt.subcommand.lower())
    if namespace is None:
        refusals.append(
            f"{stmt.span.at}: subcommand {stmt.subcommand!r} is not classified;"
            " minify refuses to guess whether its subject names the estate"
        )
        return None
    subject = stmt.subject.strip()
    out.needles.add(subject)
    if namespace == "job":
        new_subject = alloc.job(unescape_job_name(subject))
    else:
        new_subject = alloc.simple(namespace, unquote_jil_value(subject))
    if new_subject not in alloc.allocated:
        # The one invariant the token guard must NOT be trusted for. A rule-5
        # trailing comment can ride INSIDE `stmt.subject` rather than as a
        # `Comment` object, and preserve mode emits the subject byte for byte.
        # It only ever vanishes because the subject is replaced by a minted
        # name, so an unminted subject reaching the renderer is a leak, not a
        # cosmetic slip. Checked, never assumed.
        refusals.append(
            f"{stmt.span.at}: subject {subject!r} did not resolve to a minted name;"
            " minify refuses to emit a subject it did not rename"
        )
        return None
    inline = stmt.job_type_inline
    if inline is not None and not validate_keep("job_type", inline):
        refusals.append(
            f"{stmt.span.at}: job_type: {inline.strip()!r} is outside the closed value"
            " space job_type is KEEP-classified for; minify refuses to ship it verbatim"
        )
        return None
    attrs: list[RawAttr] = []
    if inline is not None:
        out.kept.add(inline.strip())
    for attr in stmt.attrs:
        emitted = _transform_attr(stmt, attr, alloc, out, refusals)
        if emitted is not None:
            attrs.append(emitted)
    return stmt.model_copy(
        update={
            "subject": new_subject,
            "job_type_inline": inline.strip() if inline is not None else None,
            "attrs": attrs,
            "date_lines": [row.strip() for row in stmt.date_lines],
            "comments": [],
            # layout trivia, all of it free text (DL-161: a comment can ride a
            # value, a gap or the subject). Preserve mode reproduces trivia
            # byte for byte, which is exactly why it must be emptied HERE
            # rather than left to the renderer.
            "pre_blank_lines": [""],
            "indent": "",
            "sep": " ",
            "inline_gap": " ",
            "inline_sep": " ",
        }
    )


def _transform_attr(
    stmt: JilStatement,
    attr: RawAttr,
    alloc: _Allocator,
    out: _Transformed,
    refusals: list[str],
) -> RawAttr | None:
    decided = classify(stmt.subcommand, attr.key)
    if decided is None:
        refusals.append(
            f"{attr.span.at}: attribute {attr.key!r} is not classified"
            " (KEEP/RENAME/REPLACE/DROP); minify refuses to guess whether it names"
            " the estate"
        )
        return None
    klass, rule = decided
    if klass is Klass.DROP:
        out.needles.add(attr.raw_value.strip())
        return None
    if klass is Klass.REPLACE:
        out.needles.add(attr.raw_value.strip())
        value = rule
    elif klass is Klass.RENAME:
        out.needles.update(_rename_needles(rule, attr.raw_value))
        try:
            value = _rewrite_value(rule, attr.raw_value, alloc)
        except (ConditionParseError, ValueRewriteError) as exc:
            refusals.append(f"{attr.span.at}: {attr.key}: {exc}")
            return None
    elif _VAR_SITE_ONLY_RE.fullmatch(unquote_jil_value(attr.raw_value)):
        # The whole value is one SEM-08 substitution site. No predicate can
        # check a value the scheduler fills in at run time, and refusing would
        # reject a legal estate; renaming the GLOBAL keeps the var site and
        # leaks nothing, because the site name is all that is left.
        value = rewrite_var_sites(unquote_jil_value(attr.raw_value), alloc)
        out.kept.add(value)
    elif attr.key.lower() == "timezone":
        # SEM-35, handled before the general KEEP branch because --scrub-timezones
        # is the ANSWER to a refused zone: validating a value that is about to be
        # replaced by UTC would block the owner's own remedy.
        original = unquote_jil_value(attr.raw_value).strip()
        out.needles.add(original)  # a scrubbed zone must not survive anywhere
        if not alloc.scrubbing_timezones and not validate_keep("timezone", original):
            refusals.append(
                f"{attr.span.at}: timezone: {original!r} is outside the closed value"
                " space timezone is KEEP-classified for; minify refuses to ship it"
                " verbatim (SEM-35 accepts a canonical IANA zone key, or a POSIX"
                " offset on a known abbreviation; --scrub-timezones maps every zone"
                " to UTC)"
            )
            return None
        value = alloc.timezone(original)
        out.kept.add(value)
    else:
        if not validate_keep(attr.key, attr.raw_value):
            refusals.append(
                f"{attr.span.at}: {attr.key}: {attr.raw_value.strip()!r} is outside the"
                f" closed value space {attr.key} is KEEP-classified for; minify refuses"
                " to ship it verbatim"
            )
            return None
        value = rewrite_var_sites(attr.raw_value.strip(), alloc)
        out.kept.add(value)
    return attr.model_copy(
        update={
            "raw_value": _scrub_value_lines(value),
            "comments": [],
            "pre_blank_lines": [],
            "indent": "",
            "sep": " ",
        }
    )


def _scrub_value_lines(value: str) -> str:
    """A rule-6 continuation line's own indentation is layout, not value."""
    return "\n".join(line.strip() for line in value.split("\n"))


def _rename_needles(rule: str, value: str) -> set[str]:
    """What a RENAME attribute contributes to the leak guard.

    A condition's operators and lookbacks survive verbatim, so only its
    identifier text is a needle; everything else renames whole.
    """
    if rule != "cond":
        return {value.strip(), unquote_jil_value(value)}
    try:
        cond = parse_condition(value.strip())
    except ConditionParseError:
        return {value.strip()}
    needles: set[str] = set()
    for atom in iter_atoms(cond):
        if isinstance(atom, GlobalAtom):
            needles.update({atom.name, atom.value})
        else:
            needles.add(atom.job.name)
            if atom.job.instance:
                needles.add(atom.job.instance)
    return needles


def transform_file(jf: JilFile, alloc: _Allocator, refusals: list[str]) -> _Transformed:
    """One parsed file in, one minified file out (comments never survive)."""
    out = _Transformed(jil=jf, needles=set(), kept=set())
    statements: list[JilStatement] = []
    for stmt in jf.statements:
        out.needles.update(_comment_text(stmt.comments))
        for attr in stmt.attrs:
            out.needles.update(_comment_text(attr.comments))
        emitted = _transform_statement(stmt, alloc, out, refusals)
        if emitted is not None:
            out.kept.update(row.strip() for row in emitted.date_lines)
            statements.append(emitted)
    out.needles.update(_comment_text(jf.trailing_comments))
    if statements:
        # the first statement opens the file; its blank-line separator would be
        # a leading empty line
        statements[0] = statements[0].model_copy(update={"pre_blank_lines": []})
    out.jil = jf.model_copy(
        update={
            "statements": statements,
            "trailing_comments": [],
            "eof_blank_lines": [],
            "final_newline": True,
            # CRLF is layout too, and a mixed-ending estate would otherwise
            # emit whichever style it happened to carry (rule 10)
            "newline_style": "\n",
        }
    )
    return out


def _comment_text(comments: list[Comment]) -> set[str]:
    return {c.text for c in comments}


# ------------------------------------------------------------ the leak guard

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{4,}")
_HAS_LETTER_RE = re.compile(r"[A-Za-z]")


def _tokens(values: Iterable[str]) -> set[str]:
    """Letter-bearing tokens of 4+ characters -- the guard's needle grain.

    Shorter tokens and all-digit tokens are excluded on purpose: `mo`, `03`,
    `1500` collide with kept day tokens, clock times and timing hints, and a
    guard that fires on those fires on every estate and gets switched off.
    """
    found: set[str] = set()
    for value in values:
        for match in _TOKEN_RE.finditer(value):
            token = match.group(0)
            if _HAS_LETTER_RE.search(token):
                found.add(token.lower())
    return found


def _vocabulary() -> set[str]:
    """Every token the output may carry that the estate did not author."""
    return _tokens(vocabulary_words())


def leak_findings(
    needles: set[str], kept: set[str], allocated: set[str], rendered: str
) -> list[str]:
    """Every input token that survived into the rendered output.

    The comparison is token-to-token, not needle-against-raw-text: `name` is a
    substring of the attribute key `box_name`, so a raw substring search
    refuses on every estate that writes `(name, QUANTITY=...)` in a comment.
    An output token is SUSPECT when nothing accounts for it; a needle that
    appears inside a suspect token is a leak.

    Three things account for an output token. The JIL/dsl41 VOCABULARY, which
    the estate did not author. The synthetic names this run ALLOCATED, which it
    authored itself. And the emitted KEEP values -- but only because every one
    of them passed `validate_keep` first, so its tokens are proven to lie in a
    closed space (an IANA zone, a day token, a SEM-37 keyword, a number). An
    unvalidated KEEP value used to land in this pool and silence real hits
    elsewhere in the output; it cannot any more, because it no longer reaches
    the output at all.
    """
    safe = _vocabulary() | _tokens(kept) | _tokens(allocated)
    suspect = _tokens([rendered]) - safe
    hits = sorted(
        (needle, token) for needle in _tokens(needles) for token in suspect if needle in token
    )
    return [
        f"leak guard: {needle!r} comes from the input and survives in the minified"
        f" output as {token!r}"
        for needle, token in hits
    ]


# ------------------------------------------------------------ structural verify


def _lookback_props(atom: StatusAtom | ExitCodeAtom) -> tuple[str, int | None, str] | None:
    if atom.lookback is None:
        return None
    return (atom.lookback.kind, atom.lookback.minutes, atom.lookback.raw)


def _cond_props(cond: Cond, names: NameMap) -> object:
    if isinstance(cond, And):
        return ("and", [_cond_props(c, names) for c in cond.operands])
    if isinstance(cond, Or):
        return ("or", [_cond_props(c, names) for c in cond.operands])
    if isinstance(cond, Paren):
        return ("paren", _cond_props(cond.inner, names))
    if isinstance(cond, GlobalAtom):
        return (
            "global",
            names.globals_.get(cond.name, cond.name),
            cond.op,
            names.global_values.get(cond.value, cond.value),
        )
    job = names.jobs.get(cond.job.name, cond.job.name)
    instance = names.instances.get(cond.job.instance or "", cond.job.instance)
    if isinstance(cond, ExitCodeAtom):
        return ("exitcode", job, instance, cond.op, cond.value, _lookback_props(cond))
    return ("status", job, instance, cond.status, _lookback_props(cond))


def _mapped_refs(value: str | None, table: dict[str, str]) -> str | None:
    """A reference list as lowering stored it, each member mapped.

    The same split `_rewrite_ref_list` uses, so both sides of the structural
    verify normalize identically -- a multi-line `run_calendar` on the original
    side and a comma list on the minified side must compare equal.
    """
    if value is None:
        return None
    members = [part.strip() for part in _REF_SEPARATOR_RE.split(value) if part.strip()]
    return ",".join(table.get(member, member) for member in members)


def _schedule_props(job: JobIR, names: NameMap) -> object:
    """The SEM-30 time cluster, with the two calendar references mapped."""
    schedule = job.schedule
    if schedule is None:
        return None
    dumped = schedule.model_dump(mode="json")
    for key in ("run_calendar", "exclude_calendar"):
        value = dumped.get(key)
        if isinstance(value, str):
            dumped[key] = _mapped_refs(value, names.calendars)
    zone = dumped.get("timezone")
    if isinstance(zone, str):
        dumped["timezone"] = names.timezones.get(zone, zone)
    return dumped


def _exec_props(job: JobIR, names: NameMap) -> object:
    """The exec cluster minify still carries: placement, and the FW lane.

    `command` compares as PRESENCE only -- REPLACE swaps its text by design --
    and the DROP'd exec attributes (owner, profile, std_*) are absent from the
    minified side by design, so comparing them would fail on every estate.
    """
    spec = job.exec_
    if spec is None:
        return None
    machine = _mapped_refs(spec.machine, names.machines)
    if isinstance(spec, FwSpec):
        return (
            "fw",
            machine,
            names.watch_files.get(spec.watch_file, spec.watch_file),
            spec.watch_interval,
            spec.watch_file_min_size,
        )
    return ("cmd", machine, spec.command is not None)


def _job_props(job: JobIR, names: NameMap) -> dict[str, object]:
    return {
        "job_type": job.job_type,
        "exec": _exec_props(job, names),
        "box_name": names.jobs.get(job.box.box_name or "", job.box.box_name),
        "box_terminator": job.box.box_terminator,
        "job_terminator": job.box.job_terminator,
        "conditions": {attr: _cond_props(cond, names) for attr, cond, _ in job.iter_conditions()},
        "resources": [
            (names.resources.get(r.name, r.name), r.quantity, r.free) for r in job.resources
        ],
        "schedule": _schedule_props(job, names),
    }


def _mapped_names(original: Iterable[str], table: dict[str, str]) -> list[str]:
    return sorted(table.get(name, name) for name in original)


def verify_findings(before: CatalogIR, after: CatalogIR, names: NameMap) -> list[str]:
    """Every way the minified estate is not isomorphic to the original.

    Each finding names the job (or the entity class) and the property, so the
    answer says what broke rather than that something did.
    """
    from dsl41.derive import derive_graph

    findings: list[str] = []
    expected = {names.jobs.get(n, n): _job_props(j, names) for n, j in before.jobs.items()}
    actual = {n: _job_props(j, NameMap()) for n, j in after.jobs.items()}
    for missing in sorted(set(expected) - set(actual)):
        findings.append(f"verify: job {missing!r} is missing from the minified estate")
    for extra in sorted(set(actual) - set(expected)):
        findings.append(f"verify: job {extra!r} appears only in the minified estate")
    for name in sorted(set(expected) & set(actual)):
        for prop in sorted(expected[name]):
            if expected[name][prop] != actual[name][prop]:
                findings.append(
                    f"verify: job {name!r}: {prop} differs"
                    f" (original {expected[name][prop]!r}, minified {actual[name][prop]!r})"
                )
    for label, original, table, produced in (
        ("machines", before.machines, names.machines, after.machines),
        ("resources", before.resources, names.resources, after.resources),
        ("calendars", before.calendars, names.calendars, after.calendars),
        ("cycles", before.cycles, names.calendars, after.cycles),
        ("globals", before.globals_declared, names.globals_, after.globals_declared),
        ("instances", before.external_instances, names.instances, after.external_instances),
    ):
        want = _mapped_names(original, table)
        if want != sorted(produced):
            findings.append(
                f"verify: {label} differ (original {want!r}, minified {sorted(produced)!r})"
            )
    want_globals = {
        names.globals_.get(name, name): names.global_values.get(value, value)
        for name, value in before.globals_declared.items()
    }
    if want_globals != after.globals_declared:
        findings.append(
            f"verify: global values differ (original {want_globals!r},"
            f" minified {after.globals_declared!r})"
        )
    want_mutex = sorted(
        sorted(names.jobs.get(n, n) for n in group) for group in derive_graph(before).mutex_groups
    )
    got_mutex = sorted(sorted(group) for group in derive_graph(after).mutex_groups)
    if want_mutex != got_mutex:
        findings.append(
            f"verify: mutex groups differ (original {want_mutex!r}, minified {got_mutex!r})"
        )
    return findings


# ------------------------------------------------------------ the entry point


@dataclass
class MinifyResult:
    """One rendered body per input file, in the order they were given."""

    bodies: list[str]
    names: NameMap

    def kept_timezones(self) -> list[str]:
        """The distinct SEM-35 zones the output still carries, if any.

        A zone discloses a region. The CLI says which ones out loud so the
        owner sees it before handing the file over, rather than finding it.
        """
        return sorted({mapped for mapped in self.names.timezones.values()})


def minify_files(
    files: list[JilFile], *, verify: bool = True, scrub_timezones: bool = False
) -> MinifyResult:
    """Minify one parsed estate. Raises `MinifyRefusal` on any refusal.

    The order of the checks is the order in which a failure is cheapest to
    explain: classification first (it changes nothing), then the round trip,
    then the structural verify, then the leak guard last -- the guard reads the
    final bytes, so it has to.
    """
    boxes, job_boxes = _box_membership(files)
    alloc = _Allocator(boxes, job_boxes, scrub_timezones=scrub_timezones)
    refusals: list[str] = []
    transformed = [transform_file(jf, alloc, refusals) for jf in files]
    if refusals:
        raise MinifyRefusal(refusals)
    bodies = [render_preserve(t.jil) for t in transformed]
    _check_roundtrip(bodies, [t.jil for t in transformed])
    if verify:
        _check_structure(files, bodies, alloc.names)
    needles: set[str] = set()
    kept: set[str] = set()
    for t in transformed:
        needles |= t.needles
        kept |= t.kept
    hits = leak_findings(needles, kept, alloc.allocated, "\n".join(bodies))
    if hits:
        raise MinifyRefusal(hits)
    return MinifyResult(bodies=bodies, names=alloc.names)


def _shape(jf: JilFile) -> list[tuple[str, str, tuple[tuple[str, str], ...], tuple[str, ...]]]:
    """A file's statements as (subcommand, subject, attrs, date rows).

    Everything that carries estate text and nothing that carries layout.
    """
    return [
        (
            stmt.subcommand,
            stmt.subject,
            tuple((a.key, a.raw_value) for a in stmt.attrs),
            tuple(stmt.date_lines),
        )
        for stmt in jf.statements
    ]


def _comment_count(jf: JilFile) -> int:
    return (
        len(jf.trailing_comments)
        + sum(len(s.comments) for s in jf.statements)
        + sum(len(a.comments) for s in jf.statements for a in s.attrs)
    )


def _check_roundtrip(bodies: list[str], emitted: list[JilFile]) -> None:
    """Re-scan the bytes and check they say what the transform meant.

    Preserve mode reproduces an AST byte for byte, so F1 alone proves nothing
    here -- a value that re-scans as a comment round-trips happily. What must
    hold is that the EMITTED bytes parse back to the same statements, keys and
    values, and that they carry no comment at all. That is the check that
    catches a rewritten value which re-opens a rule-5 block comment, or an
    inline `job_type` pair that re-scans into the subject.
    """
    for body, jf in zip(bodies, emitted, strict=True):
        try:
            again = parse_jil(body, file=jf.file)
        except JilParseError as exc:
            raise MinifyRefusal(
                [f"{jf.file}: the minified estate does not re-parse: {exc}"]
            ) from exc
        if _comment_count(again) or _shape(again) != _shape(jf):
            raise MinifyRefusal(
                [
                    f"{jf.file}: the minified estate does not re-scan to what minify"
                    " emitted (a value re-read as a comment or a key)"
                ]
            )


def _check_structure(files: list[JilFile], bodies: list[str], names: NameMap) -> None:
    try:
        before = lower_catalog(files)
    except LoweringError as exc:
        raise MinifyRefusal(
            [f"the ORIGINAL estate does not lower, so verify cannot run: {exc}", "use --no-verify"]
        ) from exc
    try:
        after = lower_catalog(
            [parse_jil(body, file=jf.file) for body, jf in zip(bodies, files, strict=True)]
        )
    except LoweringError as exc:
        raise MinifyRefusal([f"the MINIFIED estate does not lower: {exc}"]) from exc
    findings = verify_findings(before, after, names)
    if findings:
        raise MinifyRefusal(findings)


def output_paths(inputs: list[Path], out_dir: Path) -> list[Path]:
    """One output path per input, by basename. A collision is a refusal: two
    inputs would otherwise silently overwrite one another."""
    seen: dict[str, Path] = {}
    targets: list[Path] = []
    for path in inputs:
        if path.name in seen:
            raise MinifyRefusal(
                [f"--out: {seen[path.name]} and {path} share the basename {path.name!r}"]
            )
        seen[path.name] = path
        targets.append(out_dir / path.name)
    return targets
