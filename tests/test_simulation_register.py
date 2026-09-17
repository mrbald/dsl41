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

from datetime import datetime, timedelta
from typing import Literal, get_args, get_origin

import pytest

from dsl41 import ast_jil, autocal, capacity, conditions, ir, oracle, runner_preflight
from dsl41.conditions import Lookback, parse_condition
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, EventKind, JobStatus, ReleasePolicy
from dsl41.period import RuntimeProfile
from dsl41.runner_adapters import FakeAdapter
from dsl41.simulation_register import (
    FREE_SURFACES,
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


def grammar_terminals() -> set[str]:
    """`NAME=literal` per quoted alternative; bare `NAME` for a regex-only
    terminal. `%import`ed terminals (INT, WS) define nothing here and are
    deliberately out: WS is `%ignore`d and can never surface as a token."""
    out: set[str] = set()
    for name, body in _terminal_bodies().items():
        stripped = re.sub(r"/(?:[^/\\]|\\.)*/", " ", body)
        literals = re.findall(r'"((?:[^"\\]|\\.)*)"', stripped)
        if literals:
            out |= {f"{name}={literal.lower()}" for literal in literals}
        else:
            out.add(name)
    return out


# ------------------------------------------------------------------ domains


def _failed_causes() -> set[str]:
    """Every `Failed("<literal>")` cause the adapter layer can produce."""
    out: set[str] = set()
    for name in ("runner_adapters.py", "runner_startup.py"):
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "Failed" and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        out.add(arg.value)
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
        "cal_action": set(autocal._ACTIONS),
        "event": set(get_args(EventKind)),
        "status": set(get_args(JobStatus)),
        "timer": set(oracle.TIMER_CHECKS) | {"deferred_cause"},
        "profile_field": set(RuntimeProfile.model_fields),
        "profile_alt": _literal_alternatives(),
        "adapter_outcome": (
            {"int", "Terminated", "Failed"} | {f"Failed={c}" for c in _failed_causes()}
        ),
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
}

BASE_FIXTURE: dict[str, str] = {
    "jil": "insert_machine: M0\ntype: a\nnode_name: localhost\n\n"
    "insert_job: J0\njob_type: c\ncommand: true\nmachine: M0\n",
    "cond": "",
    "scenario": "insert_machine: M0\ntype: a\nnode_name: localhost\n\n"
    "insert_job: J0\njob_type: c\ncommand: true\nmachine: M0\n--\n",
    "profile": "{}",
    "outcome": "int",
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
    from lark import Lark, Token

    parser = Lark(_grammar_text(), start="start", parser="lalr", keep_all_tokens=True)
    tree = parser.parse(text)
    return [(t.type, t.value) for t in tree.scan_values(lambda v: isinstance(v, Token))]


def detect(surface: str, member: str, kind: str, text: str) -> bool:
    """Whether `text` exposes `member` of `surface`. One generic detector per
    surface: the register never gets a per-row detector, because a per-row
    detector can be written to pass."""
    if kind == "outcome":
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
        return any(
            v.strip().lower() == member
            for key in ("non_workday", "holiday")
            for v in _attr_values(text, CALENDAR_SUBCOMMANDS, key)
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


def _detect_lowered(surface: str, member: str, text: str) -> bool:
    catalog = ir.lower_source(text)
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
            assert "no label" in row.effect, f"{row.id}: no label and no 'no label' in effect"
            assert re.search(r"\b(DL|SEM)-\d+", row.cite), f"{row.id}: unlabelled, uncited"


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


def runbook_headings() -> list[str]:
    return re.findall(r"^### (.+)$", RUNBOOK.read_text(encoding="utf-8"), re.M)


def test_stated_protocols_exist_in_the_runbook() -> None:
    """Breaks when a row points at a live-instance protocol nobody wrote."""
    headings = runbook_headings()
    for row in REGISTER:
        if row.protocol is not None:
            assert any(row.protocol in h for h in headings), f"{row.id}: {row.protocol!r}"


# ------------------------------------------------------------------- 3. scope

MEMBER_ROWS = tuple(row for row in REGISTER if row.facet == "" and row.surface not in FREE_SURFACES)


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
        assert head in ("int", "Terminated", "Failed"), f"{row.id}: {text!r}"
    else:
        raise AssertionError(f"{row.id}: unknown fixture kind {kind!r}")


#: The year the calendar gate generates over, and the user it preflights as.
#: Neither is a clock read: both are fixed so the gate cannot drift with the
#: calendar date or the machine it runs on.
GEN_FROM = datetime(2026, 1, 1).date()
GEN_TO = datetime(2026, 12, 31).date()
FIXTURE_USER = "register_fixture_user"

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
        for name, job in catalog.jobs.items():
            items = runner_preflight._resource_preflight(name, job, catalog)
            items += runner_preflight._owner_preflight(name, job, FIXTURE_USER)
            refused_here += [
                f"preflight: {item.message}" for item in items if item.severity == "ERROR"
            ]
    if row.klass is Klass.REFUSED:
        assert refused_here, f"{row.id}: nothing refuses either fixture"
    else:
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


def _breadth_tokens() -> list[str]:
    """Every calendar token the autocal suites exercise, as literals."""
    text = (REPO / "tests" / "test_autocal_breadth.py").read_text(encoding="utf-8")
    text += (REPO / "tests" / "test_autocal.py").read_text(encoding="utf-8")
    return sorted({m.upper() for m in re.findall(r"\b([A-Za-z]+(?:#[A-Za-z0-9]+)?)\b", text)})


def test_classify_token_agrees_with_the_parser() -> None:
    """Breaks when the lifted keyword/family tables stop describing what
    `_parse_token` actually accepts."""
    checked = 0
    for token in _breadth_tokens():
        classified = autocal.classify_token(token)
        try:
            autocal._parse_token("C", token)
        except autocal.CalendarRuleError:
            accepted = False
        else:
            accepted = True
        if accepted:
            assert classified is not None and classified[0] in ("keyword", "family"), token
            checked += 1
        else:
            assert classified is None or classified[0] == "defective", token
    assert checked > 20, "the token inventory got too small to prove anything"


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


def test_the_fourth_timer_shape_is_the_deferred_cause() -> None:
    """Breaks when the run_window defer stops carrying `deferred_cause` --
    the `timer:deferred_cause` row derives from that literal."""
    assert "deferred_cause" in inspect.getsource(Oracle._schedule_timer)


def test_res_types_is_the_set_preflight_refuses_against() -> None:
    """Breaks when preflight grows its own spelling of R/D/T again."""
    source = inspect.getsource(runner_preflight._resource_preflight)
    assert "RES_TYPES" in source
