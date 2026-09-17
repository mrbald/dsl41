"""The simulation coverage register's own gate (DL-209).

Three obligations, each with its own group of tests below.

1. **Domain.** Every derived surface's member set is computed here from the
   code's own inventories -- an attribute constant, a Literal, the condition
   grammar file, the autocal tables. A member with no row fails, and so does
   a row whose member the code no longer has. That is the whole point of the
   register: it must not be able to agree with the code by construction.
2. **Integrity.** Ids are unique, every PROVISIONAL row names its question
   or says it has none, every `PENDING:` marker in `src/dsl41` has a row and
   every marked row has a marker, and every stated runbook protocol exists.
3. **Scope.** Every row's fixtures are well-formed, and a member row on a
   derived surface is proven by its surface's generic detector: the
   behaviour is visible in the trigger fixture and absent from the quiet
   one. Facet and free-surface rows have no detector yet (S2+), so they are
   held to well-formedness only.

Trace-name conventions (`test_semXX_*`, `test_pMxx_*`) do not apply here:
these are not semantic traces, they are the register's own integrity gate.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import pathlib
import re
import textwrap

from datetime import datetime, timedelta
from functools import cache
from typing import Any, Literal, get_args, get_origin

import pytest

from dsl41 import (
    ast_jil,
    oracle_state,
    autocal,
    capacity,
    conditions,
    ir,
    oracle,
    runner_adapters,
    runner_preflight,
    runner_wrapper,
)
from dsl41.conditions import Lookback, parse_condition
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, EventKind, JobStatus, ReleasePolicy
from dsl41.period import RuntimeProfile
from dsl41.runner_adapters import FakeAdapter
from dsl41 import simulation_register_rows as rows_mod
from dsl41.simulation_register import (
    FREE_SURFACES,
    UNREACHABLE,
    REGISTER,
    SURFACES,
    Behaviour,
    Klass,
    by_id,
    render_markdown,
    rows_for,
)

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "dsl41"
DOC = REPO / "docs" / "simulation-coverage.md"
RUNBOOK = REPO / "docs" / "live-instance-runbook.md"

T0 = datetime(2026, 7, 1, 8, 0)


# ------------------------------------------------------------------ AST scan

#: Which surface a lowering function's attribute reads belong to. Every
#: `_lower_*` in the scanned files MUST be here (a new one fails below), and
#: so must every helper that reads attribute names on their behalf.
FUNCTION_SURFACE: dict[str, str] = {
    "_collect_attrs": "job_attr",
    "_lower_job": "job_attr",
    "_job_type": "job_attr",
    "_box_linkage": "job_attr",
    "_semantics": "job_attr",
    "_schedule": "job_attr",
    "_exec_spec": "job_attr",
    "_parse_resources": "job_attr",
    "job_load_units": "job_attr",
    "priority_value": "job_attr",
    "_lower_machine": "machine_attr",
    "max_load_units": "machine_attr",
    "_node_name": "machine_attr",
    "_lower_resource": "resource_attr",
    "capacity_units": "resource_attr",
    "_lower_xinst": "xinst_attr",
    "_lower_global": "global_attr",
    "_lower_calendar": "calendar_attr",
    "_lower_cycle": "calendar_attr",
}

SCANNED_FILES = ("ir.py", "runner_preflight.py")

#: Keys the scan must keep finding. A refactor that silently stops reading
#: attribute names would otherwise shrink every derived domain at once and
#: leave the register green over a hole.
SCAN_SELF_CHECK = (
    "node_name",
    "max_load",
    "amount",
    "type",
    "machine",
    "date_conditions",
    "xtype",
    "value",
    "res_type",
    "resources",
    "command",
    "job_type",
    "start_times",
)


def _receiver_tail(node: ast.expr) -> str:
    """The last name in an attribute/subscript/call chain, lowercased."""
    while True:
        if isinstance(node, ast.Attribute):
            return node.attr.lower()
        if isinstance(node, ast.Name):
            return node.id.lower()
        if isinstance(node, (ast.Subscript, ast.Call)):
            node = node.value if isinstance(node, ast.Subscript) else node.func
            continue
        return ""


def _is_attr_receiver(node: ast.expr) -> bool:
    tail = _receiver_tail(node)
    return "attrs" in tail or "passthrough" in tail


def _string_constants(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return {s for elt in node.elts for s in _string_constants(elt)}
    return set()


def _scan_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Attribute names this function reads: `.pop`/`.get` first arguments and
    subscripts on an attrs-shaped receiver, `key ==/!=/in` comparands, and
    `_int_attr(mapping, key, ...)`'s second positional."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if sub.func.attr in ("pop", "get") and _is_attr_receiver(sub.func.value) and sub.args:
                found |= _string_constants(sub.args[0])
        # a local helper that takes the key by name (`take("start_times")`)
        # reads the attribute just as `attrs.pop` does
        if isinstance(sub, ast.Call) and sub.args:
            callee = sub.func.id if isinstance(sub.func, ast.Name) else ""
            if callee.endswith(("take", "pop", "get")):
                found |= _string_constants(sub.args[0])
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            if sub.func.id == "_int_attr" and len(sub.args) >= 2:
                found |= _string_constants(sub.args[1])
        if isinstance(sub, ast.Subscript) and _is_attr_receiver(sub.value):
            found |= _string_constants(sub.slice)
        if isinstance(sub, ast.Compare) and isinstance(sub.left, ast.Name):
            if sub.left.id == "key":
                for op, comparand in zip(sub.ops, sub.comparators):
                    if isinstance(op, (ast.Eq, ast.NotEq, ast.In)):
                        found |= _string_constants(comparand)
    return found


def scan_attribute_reads() -> dict[str, set[str]]:
    """function name -> the attribute names it reads, over SCANNED_FILES."""
    out: dict[str, set[str]] = {}
    for name in SCANNED_FILES:
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                hits = _scan_function(node)
                if hits:
                    out.setdefault(node.name, set()).update(hits)
    return out


def lowering_functions() -> set[str]:
    out: set[str] = set()
    for name in SCANNED_FILES:
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_lower_"):
                    out.add(node.name)
    return out


def _scanned(surface: str) -> set[str]:
    scan = scan_attribute_reads()
    return {
        key
        for function, keys in scan.items()
        if FUNCTION_SURFACE.get(function) == surface
        for key in keys
    }


# ------------------------------------------------------------------ grammar


def _grammar_text() -> str:
    return conditions._grammar_text()


@cache
def _lex_parser() -> Any:
    """The grammar built so every token survives to the tree -- the anonymous
    punctuation terminals are filtered out of an ordinary parse."""
    from lark import Lark

    return Lark(_grammar_text(), start="start", parser="lalr", keep_all_tokens=True)


def grammar_rules() -> set[str]:
    """Rule heads (with the inlining `?` stripped) and alias names."""
    text = _grammar_text()
    rules = set(re.findall(r"^\??([a-z_][a-z0-9_]*)\s*:", text, re.M))
    return rules | set(re.findall(r"->\s*([a-z_][a-z0-9_]*)", text))


def _terminal_bodies() -> dict[str, str]:
    """Terminal name -> its definition, continuation lines joined."""
    bodies: dict[str, str] = {}
    current: str | None = None
    for line in _grammar_text().splitlines():
        head = re.match(r"^([A-Z][A-Z0-9_]*)(?:\.\d+)?\s*:(.*)$", line)
        if head is not None:
            current = head.group(1)
            bodies[current] = head.group(2)
        elif current is not None and re.match(r"^\s+\|", line):
            bodies[current] += " " + line.strip()
        elif line.strip() and not line.strip().startswith("//"):
            current = None
    return bodies


def _terminal_literals() -> dict[str, list[str]]:
    """Terminal name -> its quoted alternatives, regex bodies removed first
    (`QUOTED`'s own regex contains a quote character)."""
    out: dict[str, list[str]] = {}
    for name, body in _terminal_bodies().items():
        stripped = re.sub(r"/(?:[^/\\]|\\.)*/", " ", body)
        out[name] = re.findall(r'"((?:[^"\\]|\\.)*)"', stripped)
    return out


def grammar_terminals() -> set[str]:
    """`NAME=literal` per quoted alternative; bare `NAME` for every other
    terminal. The names come from the built parser, not the grammar text, so
    the `%import`ed ones (INT, WS) and the anonymous ones lark builds from
    quoted punctuation inside the rules (LPAR, RPAR, COMMA, CIRCUMFLEX) are
    in the domain too -- reading the text alone made six terminals
    invisible."""
    literals = _terminal_literals()
    out: set[str] = set()
    for name in (terminal.name for terminal in _lex_parser().terminals):
        alternatives = literals.get(name) or []
        if alternatives:
            out |= {f"{name}={literal.lower()}" for literal in alternatives}
        else:
            out.add(name)
    return out


# ------------------------------------------------------------------ domains


def _cause_template(node: ast.expr, enclosing: str) -> str:
    """One `Failed(...)`/`Terminated(...)` argument as a template: a constant
    is itself, an f-string is its constant parts with `{}` where a value
    goes, and anything else is `<dynamic:...>` named by the function that
    builds it. A cause space enumerated only by its string LITERALS is two
    members wide and looks complete; this is the whole space."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "{}"
            for part in node.values
        )
    return f"<dynamic:{enclosing}>"


#: Where an adapter result is CONSTRUCTED. Held to the package by
#: `test_no_other_module_builds_an_adapter_outcome`: a hard-coded file list
#: that nothing checks is a domain that shrinks when the code moves.
OUTCOME_FILES = ("runner_adapters.py", "runner_startup.py")


def _outcome_templates() -> set[str]:
    """`Kind=template` for every `Failed(`/`Terminated(` call site."""
    out: set[str] = set()
    for name in OUTCOME_FILES:
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        stack: list[str] = []

        class Walker(ast.NodeVisitor):
            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            def _func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_FunctionDef = _func  # type: ignore[assignment]
            visit_AsyncFunctionDef = _func  # type: ignore[assignment]

            def visit_Call(self, node: ast.Call) -> None:
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("Failed", "Terminated") and node.args:
                        template = _cause_template(node.args[0], ".".join(stack))
                        out.add(f"{node.func.id}={template}")
                self.generic_visit(node)

        Walker().visit(tree)
    return out


#: Literals a dedicated surface already owns, and protocol vocabularies the
#: register deliberately does not cover (the frozen concurrency and
#: supervisor contracts close those with their own obligations, DL-209).
#: Anything else of that shape belongs to `literal_alt`.
LITERAL_ALT_EXCLUDED = frozenset(
    {
        "conditions.CmpOp",  # cond_terminal:CMP_OP=*
        "conditions.Lookback.kind",  # lookback_kind
        "conditions.Status",  # atom_status
        "capacity.DemandMode",  # demand_mode
        "ir.InitialStatus",  # initial_status
        "ir.ResourceRef.free",  # free_code
        "oracle_state.EventKind",  # event
        "oracle_state.HostState",  # a host-protocol vocabulary
        "oracle_state.JobStatus",  # status
        "oracle_state.ReleasePolicy",  # release_policy
        "runner_preflight.MachineVerdict",  # machine_verdict
        "oracle_state.EventSource",  # event_source
        "autocal.ActionCategory",  # cal_action, whose members are its pairs
    }
)

LITERAL_ALT_MODULES = (
    "ir",
    "conditions",
    "oracle_state",
    "oracle",
    "capacity",
    "runner_preflight",
    "runner_adapters",
    "runner_scheduler",
    "timezones",
    "autocal",
)


def _literal_alternatives_of(node: ast.expr) -> list[str]:
    """The alternatives if `node` is `Literal[...]` or `Literal[...] | None`."""
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if node.value.id == "Literal":
            inner = node.slice
            elts = inner.elts if isinstance(inner, ast.Tuple) else [inner]
            return [e.value for e in elts if isinstance(e, ast.Constant)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for side in (node.left, node.right):
            if found := _literal_alternatives_of(side):
                return found
    return []


def _literal_alts() -> set[str]:
    """`Name=alt` for a module-level Literal alias, `Class.field=alt` for an
    annotated class attribute, over the estate-facing modules. Any class
    shape counts -- pydantic model, dataclass or NamedTuple -- because the
    alternatives are closed the same way in each."""
    out: set[str] = set()
    for module in LITERAL_ALT_MODULES:
        tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    alts = _literal_alternatives_of(node.value)
                    key = f"{module}.{target.id}"
                    if alts and key not in LITERAL_ALT_EXCLUDED:
                        out |= {f"{target.id}={alt}" for alt in alts}
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        alts = _literal_alternatives_of(stmt.annotation)
                        key = f"{module}.{node.name}.{stmt.target.id}"
                        if alts and key not in LITERAL_ALT_EXCLUDED:
                            out |= {f"{node.name}.{stmt.target.id}={alt}" for alt in alts}
    return out


def test_no_other_module_builds_an_adapter_outcome() -> None:
    """Breaks when a `Failed(`/`Terminated(` moves to or appears in a module
    the template scan does not read -- which would drop its cause from the
    domain without dropping the behaviour from the code."""
    elsewhere: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        if path.name in OUTCOME_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("Failed", "Terminated") and node.args:
                    elsewhere.append(f"{path.name}:{node.lineno}")
    assert elsewhere == [], f"adapter outcomes built outside {OUTCOME_FILES}: {elsewhere}"


def _keyword_constants(keyword: str) -> set[str]:
    """Every string constant passed as `<keyword>=` anywhere in the package."""
    out: set[str] = set()
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == keyword and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        out.add(kw.value.value)
    return out


def _trace_markers() -> set[str]:
    """Every constant marker `Oracle._record` writes: a trace line that is
    not an `OLD->NEW` status transition."""
    tree = ast.parse((SRC / "oracle.py").read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "_record" and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if "->" not in arg.value:
                        out.add(arg.value)
    return out


def _preflight_codes() -> set[str]:
    """Every constant `code=` a `PreflightItem` is built with."""
    tree = ast.parse((SRC / "runner_preflight.py").read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "PreflightItem":
                for kw in node.keywords:
                    if kw.arg == "code" and isinstance(kw.value, ast.Constant):
                        if isinstance(kw.value.value, str):
                            out.add(kw.value.value)
    return out


def _literal_alternatives() -> set[str]:
    out: set[str] = set()
    for name, field in RuntimeProfile.model_fields.items():
        if get_origin(field.annotation) is Literal:
            out |= {f"{name}={alt}" for alt in get_args(field.annotation)}
    return out


def _cal_operator_domain() -> set[str]:
    return (
        set(autocal._GROUP_CHARS)
        | set(autocal._BINARY_OPS)
        | set(autocal._UNARY_OPS)
        | {autocal._EXCLUSION_PREFIX, autocal._RULE_SEPARATOR}
    )


def surface_domains() -> dict[str, set[str]]:
    """Every derived surface's member set, computed from code objects."""
    return {
        "statement": set(ast_jil.SUBCOMMANDS),
        "job_attr": (
            set(ir.ANNOTATION_ATTRS)
            | set(ir.PASSTHROUGH_ALLOWED)
            | set(ir.TIME_CLUSTER)
            | set(ir.EXEC_BASE_ATTRS)
            | set(ir._BOX_INERT_ATTRS)
            | _scanned("job_attr")
        ),
        "machine_attr": (set(ir._Lowerer._MACHINE_MEMBER_ATTRS) | _scanned("machine_attr") | {"*"}),
        "resource_attr": _scanned("resource_attr") | {"*"},
        "xinst_attr": _scanned("xinst_attr") | {"*"},
        "global_attr": _scanned("global_attr"),
        "calendar_attr": set(autocal.CALENDAR_ATTRS) | _scanned("calendar_attr"),
        "job_type": set(ir._JOB_TYPE_MAP),
        "bool_spelling": set(ir._TRUTHY) | set(ir._FALSY),
        "day_token": set(ir._DAY_TOKENS) | set(ir._DAY_FULL),
        "initial_status": set(get_args(ir.InitialStatus)),
        "machine_type": set(runner_preflight._KNOWN_MACHINE_TYPES),
        "res_type": set(capacity.RES_TYPES),
        "free_code": set(capacity.FREE_CODES),
        "release_policy": set(get_args(ReleasePolicy)),
        "cond_rule": grammar_rules(),
        "cond_terminal": grammar_terminals(),
        "lookback_kind": set(get_args(Lookback.model_fields["kind"].annotation)),
        "atom_status": set(get_args(conditions.Status)),
        "cal_keyword": set(autocal._KEYWORDS),
        "cal_family": (
            {name for name, _, _ in autocal._FAMILIES}
            | {name for name, _ in autocal._DEFECTIVE_FAMILIES}
        ),
        "cal_operator": _cal_operator_domain(),
        "cal_action": {
            f"{category}:{code}"
            for category in autocal._ACTION_CATEGORIES
            for code in autocal._ACTIONS
        },
        "event": set(get_args(EventKind)),
        "status": set(get_args(JobStatus)),
        "timer": set(oracle.TIMER_CHECKS) | {oracle.DEFERRED_TIMER_KEY},
        "profile_field": set(RuntimeProfile.model_fields),
        "profile_alt": _literal_alternatives(),
        "adapter_outcome": {kind.__name__ for kind in get_args(runner_adapters.AdapterResult)}
        | _outcome_templates(),
        "event_source": set(get_args(oracle_state.EventSource)),
        "trace_marker": _trace_markers(),
        "preflight_code": _preflight_codes(),
        "demand_mode": set(get_args(capacity.DemandMode)),
        "machine_verdict": set(get_args(runner_preflight.MachineVerdict)),
        "wrapper_outcome": set(get_args(runner_wrapper.WrapperOutcome)),
        "cal_workday_form": {name for name, _match, _build in autocal._WORKDAY_FORMS},
        "cal_row_form": {name for name, _f, _c, _fmt in autocal._ROW_TIME_FORMS},
        "literal_alt": _literal_alts(),
    }


DERIVED_SURFACES = tuple(s for s in SURFACES if s not in FREE_SURFACES)


# ------------------------------------------------------------------ fixtures

SURFACE_KIND: dict[str, str] = {
    "statement": "jil",
    "job_attr": "jil",
    "machine_attr": "jil",
    "resource_attr": "jil",
    "xinst_attr": "jil",
    "global_attr": "jil",
    "calendar_attr": "jil",
    "job_type": "jil",
    "bool_spelling": "jil",
    "day_token": "jil",
    "initial_status": "jil",
    "machine_type": "jil",
    "res_type": "jil",
    "free_code": "jil",
    "release_policy": "jil",
    "cond_rule": "cond",
    "cond_terminal": "cond",
    "lookback_kind": "cond",
    "atom_status": "cond",
    "cal_keyword": "jil",
    "cal_family": "jil",
    "cal_operator": "jil",
    "cal_action": "jil",
    "event": "scenario",
    "status": "scenario",
    "timer": "scenario",
    "profile_field": "profile",
    "profile_alt": "profile",
    "adapter_outcome": "outcome",
    "adapter_policy": "outcome",
    "event_source": "site",
    "literal_alt": "site",
    "wrapper_outcome": "outcome",
    "cal_workday_form": "jil",
    "cal_row_form": "jil",
    "trace_marker": "scenario",
    "preflight_code": "jil",
    "demand_mode": "jil",
    "machine_verdict": "jil",
}

BASE_FIXTURE: dict[str, str] = {
    "jil": "insert_machine: M0\ntype: a\nnode_name: localhost\n\n"
    "insert_job: J0\njob_type: c\ncommand: true\nmachine: M0\n",
    "cond": "",
    "scenario": "insert_machine: M0\ntype: a\nnode_name: localhost\n\n"
    "insert_job: J0\njob_type: c\ncommand: true\nmachine: M0\n--\n",
    "profile": "{}",
    "outcome": "int",
    # a `wrapper=` fixture names a status-record outcome; `int` is the base
    # for the adapter-result surface and names none of them
    # a `site` fixture names a function; the base names one that stamps no
    # provenance at all
    "site": "oracle.Oracle._record",
}

CALENDAR_SUBCOMMANDS = frozenset({"calendar", "ext_calendar", "extended_calendar", "cycle"})


def fixture_kind(row: Behaviour) -> str:
    """The fixture kind of a row's trigger/quiet strings. A `runtime` row
    says its own in a leading `kind:` line; every other surface has one."""
    if row.surface == "runtime":
        head = row.trigger.splitlines()[0]
        assert head.startswith("kind: "), f"{row.id}: runtime rows need a `kind:` prefix line"
        return head[len("kind: ") :].strip()
    return SURFACE_KIND[row.surface]


def fixture_body(row: Behaviour, text: str) -> str:
    if row.surface == "runtime":
        return text.split("\n", 1)[1]
    return text


def quiet_of(row: Behaviour) -> str:
    if row.quiet is not None:
        return fixture_body(row, row.quiet)
    return BASE_FIXTURE[fixture_kind(row)]


# ------------------------------------------------------------------ detectors


def _statements(text: str) -> list[ast_jil.JilStatement]:
    return list(ast_jil.parse(text, file="<fixture>").statements)


def _attr_keys(text: str, subcommands: frozenset[str]) -> set[str]:
    return {
        attr.key.lower()
        for stmt in _statements(text)
        if stmt.subcommand.lower() in subcommands
        for attr in stmt.attrs
    }


def _job_attr_keys(text: str) -> set[str]:
    """Job attribute names, plus the KEY= keywords inside a `resources:`
    value -- `_parse_resources` reads those by name, so they are members."""
    keys = _attr_keys(text, frozenset({"insert_job"}))
    for stmt in _statements(text):
        if stmt.subcommand.lower() != "insert_job":
            continue
        for attr in stmt.attrs:
            if attr.key.lower() == "resources":
                keys |= set(re.findall(r"([A-Za-z]+)\s*=", attr.raw_value))
    return keys


def _attr_values(text: str, subcommands: frozenset[str], key: str) -> list[str]:
    return [
        ir.unquote_jil_value(attr.raw_value)
        for stmt in _statements(text)
        if stmt.subcommand.lower() in subcommands
        for attr in stmt.attrs
        if attr.key.lower() == key
    ]


def _extended_conditions(text: str) -> list[str]:
    catalog = ir.lower_source(text)
    return [
        rule
        for cal in catalog.calendars.values()
        if cal.kind == "extended"
        for rule in cal.conditions
    ]


def _calendar_tokens(text: str) -> list[str]:
    out: list[str] = []
    for rule in _extended_conditions(text):
        for part in rule.split(autocal._RULE_SEPARATOR):
            if part.strip():
                out.extend(autocal._tokenize("<fixture>", part))
    return out


def _run_scenario(text: str) -> tuple[Oracle, list[str], list[str]]:
    """(oracle, emitted event kinds, script event kinds) after replay."""
    jil, _, script = text.partition("\n--\n")
    o = Oracle(ir.lower_source(jil))
    emitted: list[str] = []
    scripted: list[str] = []
    for line in script.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        payload: dict[str, object] = {}
        for token in parts[2:]:
            key, _, value = token.partition("=")
            payload[key] = int(value) if value.lstrip("-").isdigit() else value
        scripted.append(parts[1])
        event = Event(
            at=T0 + timedelta(minutes=float(parts[0])),
            kind=parts[1],  # type: ignore[arg-type]
            payload=payload,
        )
        emitted.extend(e.kind for e in o.feed(event))
    return o, emitted, scripted


def _cond_tree_rules(text: str) -> set[str]:
    """Rule/alias names in a condition's parse tree, with nothing inlined."""
    from lark import Lark

    grammar = re.sub(r"^\?", "", _grammar_text(), flags=re.M)
    parser = Lark(grammar, start="start", parser="earley", propagate_positions=True)
    return {str(node.data) for node in parser.parse(text).iter_subtrees()}


def _cond_tokens(text: str) -> list[tuple[str, str]]:
    from lark import Token

    tree = _lex_parser().parse(text)
    return [(t.type, t.value) for t in tree.scan_values(lambda v: isinstance(v, Token))]


def _site_object(qualname: str) -> Any:
    """The object `module.qualname` names."""
    module, _, rest = qualname.partition(".")
    obj: Any = importlib.import_module(f"dsl41.{module}")
    for part in rest.split("."):
        obj = getattr(obj, part)
    return obj


def _site_source(qualname: str) -> str:
    """The source of `module.qualname`, for a `site` fixture."""
    return textwrap.dedent(inspect.getsource(_site_object(qualname)))


def _stamps_source(qualname: str, value: str) -> bool:
    """Whether that function stamps `source="<value>"` -- passing it to a
    call OR declaring it as its own parameter default. Both spellings are
    real stamps: `adapter` exists ONLY as a default, and a sweep that reads
    call keywords alone cannot see the provenance that marks a completion."""
    tree = ast.parse(_site_source(qualname))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "source" and isinstance(kw.value, ast.Constant):
                    if kw.value.value == value:
                        return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.args[len(args.args) - len(args.defaults) :]
            defaults = list(zip(positional, args.defaults))
            defaults += [
                (arg, default)
                for arg, default in zip(args.kwonlyargs, args.kw_defaults)
                if default is not None
            ]
            for arg, default in defaults:
                if arg.arg == "source" and isinstance(default, ast.Constant):
                    if default.value == value:
                        return True
    return False


def _declares_literal(qualname: str, member: str) -> bool:
    """Whether that module (or class) declares the member's alternative. The
    member is `Name=alt` or `Class.field=alt`, and the fixture names the
    module-qualified site that declares it."""
    field, _, alternative = member.rpartition("=")
    module = qualname.split(".")[0]
    for candidate in _literal_declarations(module):
        if candidate == f"{field}={alternative}":
            return True
    return False


@cache
def _literal_declarations(module: str) -> frozenset[str]:
    """`Name=alt` / `Class.field=alt` for every Literal this module declares,
    including the ones a dedicated surface owns -- the exclusion map is the
    register's, not the module's."""
    out: set[str] = set()
    tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                out |= {f"{target.id}={alt}" for alt in _literal_alternatives_of(node.value)}
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    out |= {
                        f"{node.name}.{stmt.target.id}={alt}"
                        for alt in _literal_alternatives_of(stmt.annotation)
                    }
    return frozenset(out)


def detect(surface: str, member: str, kind: str, text: str) -> bool:
    """Whether `text` exposes `member` of `surface`. One generic detector per
    surface: the register never gets a per-row detector, because a per-row
    detector can be written to pass."""
    if kind == "site":
        if surface == "literal_alt":
            return _declares_literal(text, member)
        return _stamps_source(text, member)
    if kind == "outcome":
        if surface == "wrapper_outcome":
            return text == f"wrapper={member}"
        return text == member
    if kind == "profile":
        data = json.loads(text)
        if surface == "profile_field":
            return member in data
        field, _, alt = member.partition("=")
        return data.get(field) == alt
    if kind == "cond":
        if not text.strip():
            return False
        if surface == "cond_rule":
            return member in _cond_tree_rules(text)
        if surface == "cond_terminal":
            name, _, literal = member.partition("=")
            if name in _lex_parser().ignore_tokens:
                # an ignored terminal is never a token; it is exposed when
                # its own pattern matches the text
                pattern = next(x for x in _lex_parser().terminals if x.name == name)
                return re.search(pattern.pattern.to_regexp(), text) is not None
            return any(
                t == name and (not literal or v.lower() == literal) for t, v in _cond_tokens(text)
            )
        cond = parse_condition(text)
        if surface == "atom_status":
            return any(
                getattr(atom, "status", None) == member for atom in conditions.iter_atoms(cond)
            )
        return any(
            getattr(atom, "lookback", None) is not None and getattr(atom, "lookback").kind == member
            for atom in conditions.iter_atoms(cond)
        )
    if kind == "scenario":
        o, emitted, scripted = _run_scenario(text)
        if surface == "event":
            return member in set(emitted) | set(scripted)
        if surface == "trace_marker":
            return any(entry.transition == member for entry in o.trace())
        if surface == "status":
            return any(entry.transition.split("->")[-1] == member for entry in o.trace())
        return any(
            event.payload.get("check") == member
            or (member == "deferred_cause" and "deferred_cause" in event.payload)
            for _, _, event in o.store.timers()
        )
    return _detect_jil(surface, member, text)


_ATTR_SURFACE_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "machine_attr": frozenset({"insert_machine"}),
    "resource_attr": frozenset({"insert_resource"}),
    "xinst_attr": frozenset({"insert_xinst"}),
    "global_attr": frozenset({"insert_global"}),
    "calendar_attr": CALENDAR_SUBCOMMANDS,
}


def _detect_jil(surface: str, member: str, text: str) -> bool:
    if surface == "statement":
        return any(stmt.subcommand.lower() == member for stmt in _statements(text))
    if surface == "job_attr":
        return member in _job_attr_keys(text)
    if surface in _ATTR_SURFACE_SUBCOMMANDS:
        keys = _attr_keys(text, _ATTR_SURFACE_SUBCOMMANDS[surface])
        if member == "*":
            return bool(keys - (surface_domains()[surface] - {"*"}))
        return member in keys
    jobs = frozenset({"insert_job"})
    if surface == "job_type":
        return any(v.lower() == member for v in _attr_values(text, jobs, "job_type"))
    if surface == "bool_spelling":
        return any(
            v.strip().lower() == member
            for key in ("date_conditions", "auto_hold")
            for v in _attr_values(text, jobs, key)
        )
    if surface == "day_token":
        return any(
            token.strip().lower() == member
            for value in _attr_values(text, jobs, "days_of_week")
            for token in value.split(",")
        )
    if surface == "initial_status":
        return any(v.strip().upper() == member for v in _attr_values(text, jobs, "status"))
    if surface == "cal_keyword":
        return any(autocal.classify_token(t) == ("keyword", member) for t in _calendar_tokens(text))
    if surface == "cal_family":
        return any(
            autocal.classify_token(t) in (("family", member), ("defective", member))
            for t in _calendar_tokens(text)
        )
    if surface == "cal_operator":
        return _detect_cal_operator(member, text)
    if surface == "cal_action":
        category, _, code = member.partition(":")
        return any(
            v.strip().lower() == code for v in _attr_values(text, CALENDAR_SUBCOMMANDS, category)
        )
    if surface == "cal_workday_form":
        return any(
            autocal.classify_workday(v) == member
            for v in _attr_values(text, CALENDAR_SUBCOMMANDS, "workday")
        )
    if surface == "cal_row_form":
        return any(
            autocal.classify_row(row) == member
            for stmt in _statements(text)
            if stmt.subcommand.lower() in CALENDAR_SUBCOMMANDS
            for row in stmt.date_lines
        )
    return _detect_lowered(surface, member, text)


def _detect_cal_operator(member: str, text: str) -> bool:
    rules = _extended_conditions(text)
    if member in autocal._UNARY_OPS or (member in autocal._BINARY_OPS and member.isalpha()):
        return any(t.lower() == member for t in _calendar_tokens(text))
    if member == autocal._EXCLUSION_PREFIX:
        return any(
            t.lower().startswith(member) and autocal.classify_token(t) is not None
            for t in _calendar_tokens(text)
        )
    return any(member in rule for rule in rules)


#: A fixed anchor and identity for the preflight detector: no clock read, no
#: hostname guess. `as_machine` is non-empty, so `_local_identity` takes
#: EXACTLY these names plus localhost.
PREFLIGHT_START = datetime(2026, 1, 1, 0, 0)
AS_MACHINE = frozenset({"m0"})
LOCAL_IDENTITY = frozenset({"localhost", "m0"})


def _preflight_items(catalog: ir.CatalogIR) -> list[runner_preflight.PreflightItem]:
    """Preflight under BOTH machine policies, de-duplicated: `machine-mixed`
    exists only under local-eligible and `machine` only under strict, so one
    policy alone cannot enumerate the code space -- but every other rule
    reports the same finding twice.

    The owner rule reads `getpass.getuser()`, so the `owner` fixture states a
    name no account has rather than pinning one here."""
    seen: dict[tuple[str, str, str | None, str], runner_preflight.PreflightItem] = {}
    for policy in ("strict", "local-eligible"):
        for item in runner_preflight.preflight(
            catalog,
            execution=True,
            as_machine=AS_MACHINE,
            start=PREFLIGHT_START,
            machine_policy=policy,  # type: ignore[arg-type]
        ):
            seen.setdefault((item.severity, item.code, item.job, item.message), item)
    return list(seen.values())


def _detect_lowered(surface: str, member: str, text: str) -> bool:
    catalog = ir.lower_source(text)
    if surface == "preflight_code":
        return any(item.code == member for item in _preflight_items(catalog))
    if surface == "demand_mode":
        return any(
            capacity.requirement_demand(
                capacity.resource_type(catalog.resources.get(ref.name)), ref.free
            )[0]
            == member
            for job in catalog.jobs.values()
            for ref in job.resources
        )
    if surface == "machine_verdict":
        return any(
            runner_preflight.resolve_machine(
                job.exec_.machine, catalog.machines, LOCAL_IDENTITY
            ).verdict
            == member
            for job in catalog.jobs.values()
            if job.exec_ is not None and job.exec_.machine is not None
        )
    if surface == "machine_type":
        return any((m.machine_type or "").lower() == member for m in catalog.machines.values())
    if surface == "res_type":
        return any(capacity.resource_type(r) == member for r in catalog.resources.values())
    if surface == "free_code":
        return any(ref.free == member for job in catalog.jobs.values() for ref in job.resources)
    if surface == "release_policy":
        return any(
            capacity.release_policy(
                capacity.resource_type(catalog.resources.get(ref.name)), ref.free
            )
            == member
            for job in catalog.jobs.values()
            for ref in job.resources
            if ref.name in catalog.resources
        )
    raise AssertionError(f"no detector for surface {surface!r}")


# ------------------------------------------------------------------ 1. domain


@pytest.mark.parametrize("surface", DERIVED_SURFACES)
def test_surface_domain_matches_the_code(surface: str) -> None:
    """Breaks when an inventory grows a member with no row, or when a row
    names a member the code dropped."""
    domain = surface_domains()[surface]
    members = {row.member for row in rows_for(surface) if row.facet == ""}
    assert members - domain == set(), f"{surface}: stale rows {sorted(members - domain)}"
    assert domain - members == set(), f"{surface}: no row for {sorted(domain - members)}"


def test_every_surface_has_rows() -> None:
    """Breaks when SURFACES names a surface nothing covers."""
    for surface in SURFACES:
        assert rows_for(surface), f"{surface}: no rows"


def test_facet_rows_name_a_member_of_their_surface() -> None:
    """Breaks when a facet is hung on a member the surface does not have."""
    for row in REGISTER:
        if row.facet and row.surface not in FREE_SURFACES:
            assert row.member in surface_domains()[row.surface], row.id


def test_scan_finds_the_keys_it_must_keep_finding() -> None:
    """Breaks when the AST scan stops seeing attribute reads -- which would
    shrink every derived domain at once and hide the hole."""
    found = {key for keys in scan_attribute_reads().values() for key in keys}
    assert set(SCAN_SELF_CHECK) - found == set()


def test_every_scanned_function_is_mapped_to_a_surface() -> None:
    """Breaks when a new lowering function reads attribute names and nobody
    said which surface they belong to."""
    unmapped = sorted(set(scan_attribute_reads()) - set(FUNCTION_SURFACE))
    assert unmapped == [], f"unmapped functions with attribute reads: {unmapped}"
    missing = sorted(lowering_functions() - set(FUNCTION_SURFACE))
    assert missing == [], f"_lower_* functions outside the surface map: {missing}"


# --------------------------------------------------------------- 2. integrity


def test_ids_are_unique() -> None:
    """Breaks when two rows collide on surface:member#facet."""
    assert len(by_id()) == len(REGISTER)


def test_rows_are_well_formed() -> None:
    """Breaks on an unknown surface, a zero revision, or an empty effect."""
    for row in REGISTER:
        assert row.surface in SURFACES, row.id
        assert row.revision >= 1, row.id
        assert row.effect.strip(), row.id
        assert row.member.strip(), row.id


def test_provisional_rows_name_their_question() -> None:
    """Breaks when a pinned default is recorded with neither a label nor an
    explicit statement that no label was opened for it."""
    for row in REGISTER:
        if row.klass is not Klass.PROVISIONAL:
            continue
        if row.label is None:
            assert "no label" in row.effect.lower(), f"{row.id}: no label, and no saying so"
            followable = re.search(r"\b(DL|SEM|PR)-\d+", row.cite) or CODE_CITE_RE.search(row.cite)
            assert followable, f"{row.id}: unlabelled, and the cite leads nowhere"


def test_refused_rows_cite_a_site() -> None:
    """Breaks when a refusal points at prose instead of the refusing code. A
    dotted token is not enough -- `period-model ss2.1` is dotted and is not a
    site -- so the cite must name one of the modules the code lives in, and
    `test_code_citations_resolve` then proves the name exists."""
    for row in REGISTER:
        if row.klass is Klass.REFUSED:
            assert CODE_CITE_RE.search(row.cite), f"{row.id}: {row.cite!r} names no site"


def test_bounded_rows_state_their_bound() -> None:
    """Breaks when a bounded search claims absence without saying how far."""
    for row in REGISTER:
        if row.bound is not None:
            assert row.bound in row.effect, f"{row.id}: effect does not mention {row.bound!r}"


LABEL_RE = re.compile(r"^(Q\d[a-z]?|Qr\d|E\d{1,2})$")
#: `docs/citation-index.md`'s marker shape. U-labels are excluded from the
#: counterpart check below: U1/U3b are UC-BACKEND markers (`backend_uc.py`,
#: `derive.py`), and the UC compiler is not the simulation this register
#: covers -- its refusals are the migration report's, not a runtime default.
MARKER_RE = re.compile(r"PENDING: (Q\d[a-z]?|Qr\d|U\d[a-z]?|E\d{1,2})")
#: Labels the counterpart check skips: the U-series (above) and `Q8x`, which
#: the citation index defines as "the Q8 family" -- `autocal.py`'s module
#: docstring cites it to describe the convention, not to pin one default.
SKIP_LABEL_RE = re.compile(r"^(U\d[a-z]?|Q8x)$")


def source_markers() -> dict[str, list[str]]:
    """label -> the `src/dsl41` files carrying a `PENDING:` marker for it."""
    out: dict[str, list[str]] = {}
    for path in sorted(SRC.glob("*.py")):
        for label in MARKER_RE.findall(path.read_text(encoding="utf-8")):
            out.setdefault(label, []).append(path.name)
    return out


def test_labels_match_the_citation_index_shape() -> None:
    """Breaks when a row invents a label the citation index cannot resolve."""
    for row in REGISTER:
        if row.label is not None:
            assert LABEL_RE.match(row.label), f"{row.id}: {row.label!r}"


def _enclosing_qualname(tree: ast.Module, line: int) -> str:
    """The `Class.function` (or `<module>`) whose body holds `line`."""
    best = ("<module>", -1)
    stack: list[str] = []

    class Walker(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._scope(node)

        def _func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self._scope(node)

        visit_FunctionDef = _func  # type: ignore[assignment]
        visit_AsyncFunctionDef = _func  # type: ignore[assignment]

        def _scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            nonlocal best
            stack.append(node.name)
            start = node.lineno
            end = node.end_lineno or start
            if start <= line <= end and start > best[1]:
                best = (".".join(stack), start)
            self.generic_visit(node)
            stack.pop()

    Walker().visit(tree)
    return best[0]


def marker_sites() -> dict[str, list[str]]:
    """`<label>@<module>.<qualname>` -> the lines that carry it. One entry
    per SITE, not per label: two defaults pinned under the same question are
    two behaviours, and collapsing them to the label let the second ride the
    first's row."""
    out: dict[str, list[str]] = {}
    for path in sorted(SRC.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for number, line in enumerate(source.splitlines(), start=1):
            for label in MARKER_RE.findall(line):
                if SKIP_LABEL_RE.match(label):
                    continue
                member = f"{label}@{path.stem}.{_enclosing_qualname(tree, number)}"
                out.setdefault(member, []).append(f"{path.name}:{number}")
    return out


def test_every_marker_site_is_claimed_by_exactly_one_row() -> None:
    """Breaks when a SECOND default is pinned under a label a row already
    claims. Counting labels alone hid that: one `Q8d` row covered four
    distinct pinned choices, and a fifth needed no row at all."""
    sites = marker_sites()
    claims: dict[str, list[str]] = {}
    for row in REGISTER:
        for site in row.sites:
            claims.setdefault(site, []).append(row.id)
    assert sorted(set(claims) - set(sites)) == [], (
        f"rows claim marker sites the sources do not carry: {sorted(set(claims) - set(sites))}"
    )
    assert sorted(set(sites) - set(claims)) == [], (
        f"marker sites no row claims: {sorted(set(sites) - set(claims))}"
    )
    doubled = {site: ids for site, ids in claims.items() if len(ids) > 1}
    assert doubled == {}, f"marker sites claimed by more than one row: {doubled}"


def test_marked_rows_claim_their_sites() -> None:
    """Breaks when a row says it has a code marker and names no site."""
    for row in REGISTER:
        if row.marker:
            assert row.sites, f"{row.id}: marker=True with no sites"
        if row.sites:
            assert row.marker, f"{row.id}: sites without marker=True"
            assert row.label is not None, row.id
            assert all(site.startswith(f"{row.label}@") for site in row.sites), row.id


def test_marked_rows_have_a_marker_in_the_sources() -> None:
    """Breaks when a row claims a `PENDING` marker the code does not carry."""
    markers = source_markers()
    for row in REGISTER:
        if row.marker:
            assert row.label is not None, row.id
            assert row.label in markers, f"{row.id}: no `PENDING: {row.label}` in src/dsl41"


def test_every_source_marker_has_a_row() -> None:
    """Breaks when a new pinned default is marked in the code and nothing in
    the register says what it pins."""
    labelled = {row.label for row in REGISTER if row.label is not None}
    orphans = sorted(
        label
        for label in source_markers()
        if not SKIP_LABEL_RE.match(label) and label not in labelled
    )
    assert orphans == [], f"markers with no register row: {orphans}"


def test_only_provisional_rows_carry_a_label() -> None:
    """Breaks when a label lands on a row that is not a pinned default: a
    label names an OPEN question, and a supported or refused behaviour has
    no open question to name."""
    misplaced = [row.id for row in REGISTER if row.label and row.klass is not Klass.PROVISIONAL]
    assert misplaced == [], f"labels outside the provisional class: {misplaced}"


def test_every_provisional_row_is_followable() -> None:
    """Breaks when a pinned default gives a reader nowhere to go: a labelled
    row needs a code marker or a live-instance protocol to settle it, or a
    citation that says where the default was pinned."""
    stranded = [
        row.id
        for row in REGISTER
        if row.klass is Klass.PROVISIONAL
        and not row.marker
        and row.protocol is None
        and not re.search(r"\b(DL|SEM|PR)-\d+", row.cite)
        and not CODE_CITE_RE.search(row.cite)
    ]
    assert stranded == [], f"provisional rows with no way to follow them: {stranded}"


R2_LABELS = ("Q3c", "Q3d", "Qr6", "E5", "E6", "E7", "E8", "E9", "Qr3", "Qr4", "Qr2")


@pytest.mark.parametrize("label", R2_LABELS)
def test_the_named_open_questions_each_have_a_row(label: str) -> None:
    """Breaks when one of the eleven questions this register was opened for
    loses its row."""
    assert any(row.label == label for row in REGISTER), label


#: Modules a row may cite a site inside. A cite naming one of these must
#: resolve to a real attribute -- a citation nobody can follow is not a
#: citation (`docs/citation-index.md`), and a renamed function would
#: otherwise leave a dead pointer behind.
CITED_MODULES = (
    "ast_jil",
    "autocal",
    "capacity",
    "conditions",
    "ir",
    "oracle",
    "oracle_state",
    "period",
    "runner_adapters",
    "runner_preflight",
    "runner_scheduler",
    "runner_startup",
)
CODE_CITE_RE = re.compile(r"\b(" + "|".join(CITED_MODULES) + r")((?:\.\w+)+)")


def test_code_citations_resolve() -> None:
    """Breaks when a row points at a module attribute that was renamed or
    never existed."""
    dead: list[tuple[str, str]] = []
    for row in REGISTER:
        for match in CODE_CITE_RE.finditer(row.cite):
            obj: object = importlib.import_module(f"dsl41.{match.group(1)}")
            for part in match.group(2).lstrip(".").split("."):
                obj = getattr(obj, part, None)
                if obj is None:
                    dead.append((row.id, match.group(0)))
                    break
    assert dead == [], f"citations that resolve to nothing: {dead}"


def test_pinned_patterns_equal_the_code() -> None:
    """Breaks when a regex the register pinned is edited. A family name or a
    terminal name alone says nothing about what the pattern ADMITS, so the
    row carries the pattern verbatim and this holds the two equal: the edit
    is not blocked, it just cannot land without re-reading the row."""
    families = {name: pattern.pattern for name, pattern, _b in autocal._FAMILIES}
    families |= {name: pattern.pattern for name, pattern in autocal._DEFECTIVE_FAMILIES}
    terminals = {term.name: term.pattern.to_regexp() for term in _lex_parser().terminals}
    for row in REGISTER:
        if row.pattern is None:
            continue
        if row.surface == "cal_family":
            assert row.pattern == families[row.member], row.id
        elif row.surface == "cond_terminal":
            assert row.pattern == terminals[row.member], row.id
        else:
            raise AssertionError(f"{row.id}: no pattern source for surface {row.surface!r}")


def test_every_regex_member_pins_its_pattern() -> None:
    """Breaks when a regex-recognised member joins a surface without pinning
    what its regex admits -- the hole this mechanism exists to close."""
    missing = [row.id for row in rows_for("cal_family") if row.pattern is None]
    # lark tells the two apart itself: a fixed-string terminal (the anonymous
    # punctuation) has nothing to read, a regex one does
    regex_terminals = {
        term.name for term in _lex_parser().terminals if term.pattern.type == "re"
    } - set(_lex_parser().ignore_tokens)
    for row in rows_for("cond_terminal"):
        if row.member in regex_terminals and row.pattern is None:
            missing.append(row.id)
    assert missing == [], f"regex members with no pinned pattern: {missing}"


def runbook_headings() -> list[str]:
    return re.findall(r"^### (.+)$", RUNBOOK.read_text(encoding="utf-8"), re.M)


def test_stated_protocols_exist_in_the_runbook() -> None:
    """Breaks when a row points at a live-instance protocol nobody wrote."""
    headings = runbook_headings()
    for row in REGISTER:
        if row.protocol is not None:
            assert any(row.protocol in h for h in headings), f"{row.id}: {row.protocol!r}"


# ------------------------------------------------------------------- 3. scope

MEMBER_ROWS = tuple(
    row
    for row in REGISTER
    if row.facet == "" and row.surface not in FREE_SURFACES and row.id not in UNREACHABLE
)


def test_the_unreachable_set_is_only_what_cannot_be_reached() -> None:
    """Breaks when the unreachable set grows a member a fixture COULD
    exercise. `preflight_code:job-type` is proven here: lowering maps every
    accepted job_type into the runner's executable universe, so the preflight
    gate behind it cannot fire on any JIL. `preflight_code:oracle` has no
    such one-line proof -- it guards `Oracle(catalog)` raising over a catalog
    lowering does not build -- so it is pinned as the only other member."""
    assert set(ir._JOB_TYPE_MAP.values()) <= runner_preflight._RUNNABLE_TYPES
    assert UNREACHABLE == {"preflight_code:job-type", "preflight_code:oracle"}
    for row_id in UNREACHABLE:
        assert row_id in by_id(), row_id


@pytest.mark.parametrize("row_id", [row.id for row in MEMBER_ROWS])
def test_member_rows_are_proven_by_the_generic_detector(row_id: str) -> None:
    """Breaks when a row's trigger fixture does not actually expose the
    behaviour, or its quiet fixture does."""
    row = by_id()[row_id]
    kind = fixture_kind(row)
    assert detect(row.surface, row.member, kind, fixture_body(row, row.trigger)), (
        f"{row.id}: trigger fixture does not expose the member"
    )
    assert not detect(row.surface, row.member, kind, quiet_of(row)), (
        f"{row.id}: quiet fixture exposes the member too"
    )


def _assert_well_formed(row: Behaviour, kind: str, text: str) -> None:
    if kind == "jil":
        ast_jil.parse(text, file="<fixture>")
        if row.klass is not Klass.REFUSED:
            ir.lower_source(text)
    elif kind == "cond":
        if text.strip():
            parse_condition(text)
    elif kind == "scenario":
        _run_scenario(text)
    elif kind == "profile":
        RuntimeProfile.model_validate(json.loads(text))
    elif kind == "outcome":
        head, _, _cause = text.partition("=")
        assert head in ("int", "Terminated", "Failed", "wrapper"), f"{row.id}: {text!r}"
    elif kind == "site":
        target = _site_object(text)
        if row.surface == "event_source":
            # the claim is "THIS function stamps it": a class would be
            # satisfied by anything stamped anywhere inside it
            assert inspect.isfunction(target) or inspect.ismethod(target), (
                f"{row.id}: {text!r} is not a function"
            )
        else:
            # a `literal_alt` site names where the Literal is DECLARED, which
            # is a class or a module-level function, never a call
            assert inspect.isclass(target) or inspect.isfunction(target), (
                f"{row.id}: {text!r} is not a declaring site"
            )
        ast.parse(_site_source(text))
    else:
        raise AssertionError(f"{row.id}: unknown fixture kind {kind!r}")


#: The year the calendar gate generates over, and the user it preflights as.
#: Neither is a clock read: both are fixed so the gate cannot drift with the
#: calendar date or the machine it runs on.
GEN_FROM = datetime(2026, 1, 1).date()
GEN_TO = datetime(2026, 12, 31).date()

#: The rows whose fixture produces a preflight ERROR ON PURPOSE, because the
#: ERROR IS the behaviour the row exposes. Named one by one, not by surface:
#: `preflight_code:n-retrys` and `:skeleton-cycle` are WARN rules and lose
#: nothing by staying under the gate.
PREFLIGHT_EXEMPT = frozenset(
    {
        "preflight_code:machine-mixed",
        "machine_verdict:foreign",
        "machine_verdict:mixed",
        # its QUIET half has to be a machine that is NOT local, and every
        # such estate is a preflight ERROR by construction
        "machine_verdict:local",
    }
)

JIL_ROWS = tuple(row for row in REGISTER if fixture_kind(row) == "jil")


@pytest.mark.parametrize("row_id", [row.id for row in JIL_ROWS])
def test_jil_fixtures_reach_the_engine(row_id: str) -> None:
    """Breaks when a fixture parses and lowers but the engine will not read
    it -- an extended calendar whose cyccal/holcal is missing, or an estate
    preflight refuses. A scope fixture nobody can run proves nothing. A
    REFUSED row is the mirror image: one of these steps MUST fail, or the
    row claims a refusal that does not happen."""
    row = by_id()[row_id]
    refused_here: list[str] = []
    for text in (fixture_body(row, row.trigger), quiet_of(row)):
        try:
            catalog = ir.lower_source(text)
        except ir.LoweringError as exc:
            refused_here.append(f"lowering: {exc}")
            continue
        for calendar in catalog.calendars.values():
            if calendar.kind != "extended":
                continue
            try:
                # generation, not just compile: the walk cap and the
                # disposition pipeline only refuse once days are produced
                compiled = autocal.compile_calendar(calendar, catalog)
                compiled.days_between(GEN_FROM, GEN_TO)
            except autocal.CalendarRuleError as exc:
                refused_here.append(f"calendar: {exc}")
        refused_here += [
            f"preflight: {item.code}: {item.message}"
            for item in _preflight_items(catalog)
            if item.severity == "ERROR"
        ]
    if row.id in UNREACHABLE:
        # no input reaches the gate this row describes, which is the row's
        # own claim; `test_the_unreachable_set_is_only_what_cannot_be_reached`
        # is what holds that claim honest
        return
    if row.klass is Klass.REFUSED:
        assert refused_here, f"{row.id}: nothing refuses either fixture"
    elif row.id not in PREFLIGHT_EXEMPT:
        assert refused_here == [], f"{row.id}: {refused_here}"


@pytest.mark.parametrize("row_id", [row.id for row in REGISTER])
def test_every_row_carries_well_formed_scope_fixtures(row_id: str) -> None:
    """Breaks when a fixture does not parse, does not lower, or does not run
    -- a scope fixture nobody can execute proves nothing."""
    row = by_id()[row_id]
    kind = fixture_kind(row)
    _assert_well_formed(row, kind, fixture_body(row, row.trigger))
    _assert_well_formed(row, kind, quiet_of(row))
    assert fixture_body(row, row.trigger) != quiet_of(row), f"{row.id}: trigger == quiet"


# --------------------------------------------------------------------- 4. doc

BEGIN = "<!-- register:begin -->"
END = "<!-- register:end -->"


def test_the_doc_block_is_the_rendered_register() -> None:
    """Breaks when the rows change and nobody ran
    `scripts/render_simulation_coverage.py`."""
    text = DOC.read_text(encoding="utf-8")
    block = text.split(BEGIN, 1)[1].split(END, 1)[0]
    assert block.strip("\n") == render_markdown().strip("\n")


def test_the_renderer_is_deterministic() -> None:
    """Breaks when the rendered order stops being the id order -- the one
    property that makes two renderings of the same rows identical, whatever
    order the rows module happens to list them in."""
    assert render_markdown() == render_markdown()
    rendered = render_markdown()
    for surface in SURFACES:
        printed = [
            line.split(" | ")[0].removeprefix("| ")
            for line in rendered.splitlines()
            if line.startswith(f"| {surface}:")
        ]
        ids = [row.id for row in rows_for(surface)]
        assert ids == sorted(ids), f"{surface}: rows_for is not id-ordered"
        # `_cell` escapes a pipe inside an id (`cal_operator:|`), and an
        # escaped id no longer sorts like the raw one -- compare the
        # rendered cells against the escaped ids, in the raw ids' order
        assert printed == [row_id.replace("|", "\\|") for row_id in ids], surface


def test_the_doc_keeps_its_hand_written_preamble() -> None:
    """Breaks when the generated block eats the prose above it."""
    text = DOC.read_text(encoding="utf-8")
    assert text.startswith("# Simulation coverage register\n")
    assert "modelled, refused, or listed" in text.split(BEGIN, 1)[0]


# ----------------------------------------------------------------- 5. autocal


def _register_calendar_tokens() -> list[str]:
    """Every token the register itself names: each `_KEYWORDS` key and each
    `cal_family` row's own trigger token. No sampling from test prose, and
    no "at least N" floor -- the inventory IS the register's."""
    tokens = [key.upper() for key in autocal._KEYWORDS]
    for member, (token, _pattern, _words) in rows_mod._CAL_FAMILIES.items():
        assert member in {row.member for row in rows_for("cal_family")}
        tokens.append(token)
    for member, (token, _pattern, _words) in rows_mod._DEFECTIVE_FAMILIES.items():
        assert member in {row.member for row in rows_for("cal_family")}
        tokens.append(token)
    return tokens


def test_classify_token_agrees_with_the_parser() -> None:
    """Breaks when the lifted keyword/family tables stop describing what
    `_parse_token` actually accepts, over EVERY token the register names --
    each keyword, each family's own token, and each defective token, in the
    plain and `X`-prefixed spellings."""
    for token in _register_calendar_tokens():
        for spelling in (token, f"X{token}"):
            classified = autocal.classify_token(spelling)
            try:
                autocal._parse_token("C", spelling)
            except autocal.CalendarRuleError:
                assert classified is None or classified[0] == "defective", spelling
            else:
                assert classified is not None, spelling
                assert classified[0] in ("keyword", "family"), spelling


@pytest.mark.parametrize("token", ["WORKDX1", "WORKDX12", "CWEK#1", "CWEKM2", "CWEKX3"])
def test_defective_tokens_classify_as_defective(token: str) -> None:
    """Breaks when a doc-defective family stops being refused."""
    assert autocal.classify_token(token) is not None
    assert autocal.classify_token(token)[0] == "defective"  # type: ignore[index]
    with pytest.raises(autocal.CalendarRuleError):
        autocal._parse_token("C", token)


def test_calendar_attrs_is_the_allow_list_compile_uses() -> None:
    """Breaks when `compile_calendar` grows an attribute the constant does
    not name -- which would leave the calendar surface short a member."""
    source = inspect.getsource(autocal.compile_calendar)
    assert "CALENDAR_ATTRS" in source
    for attr in autocal.CALENDAR_ATTRS - {"description"}:
        assert attr in source, attr


# --------------------------------------------------------------- 6. pins


def test_fake_adapter_completes_unscripted_runs_instantly() -> None:
    """Breaks when the rehearsal default stops being exit code 0 at once --
    the `adapter_policy:unscripted-completion` row states it."""
    default = inspect.signature(FakeAdapter.__init__).parameters["default"].default
    assert default == (0.0, 0)


def test_res_types_is_the_set_preflight_refuses_against() -> None:
    """Breaks when preflight grows its own spelling of R/D/T again."""
    source = inspect.getsource(runner_preflight._resource_preflight)
    assert "RES_TYPES" in source
