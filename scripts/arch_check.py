#!/usr/bin/env python3
"""Deterministic architecture gate (DL-75). Stdlib only, no LLM, runs in ~1s.

Blocking checks (exit 1) are objective regressions -- each one names a
specific way the tree got worse, never a matter of taste:

1. identical function bodies in two different modules (the wrapper/supervisor
   drift class DL-72 removed: copies drift, and the drift is the bug);
2. a NEW private cross-module import (`from dsl41.x import _y`) in src/ --
   the ones the tree already had are pinned in scripts/arch_baseline.json, so
   this catches additions, not history. tests/ is deliberately not scanned: a
   white-box test reaching into the module it tests is this project's normal
   style, not two modules coupling, and blocking it would red CI on an
   ordinary new test whose only remedy re-blesses src/ too;
3. a citation token in src/dsl41/*.py that resolves to no namespace row in
   docs/citation-index.md (a citation nobody can follow is not a citation),
   and -- since DL-110 -- a doc naming a `test_...` that no test defines: a
   worked example's citation is what makes it a claim rather than a story,
   and renaming a test is a refactor nobody thinks of as a doc change;
4. an IR-F model shape change without an IR_VERSION bump -- the CatalogIR
   JSON schema is hashed and pinned in IR_SCHEMA_PIN below;
5. a JobRuntime field or a global assigned outside RuntimeState (DL-82/86) --
   the concurrency model needs one observable write path, and a test that
   watches whole feeds cannot see a missed write site.

Advisory checks (reported, exit 0) are measured against the baseline, so
only NEW or WORSENED sizes surface: modules over 1200 lines, functions over
120 lines, functions over 40 branches. Taste is not a build failure.

Either way the script prints "architecture review due -- run /arch-review"
when a check trips, or when the diff since the most recent arch-review/*
tag (branch point if there is no such tag) exceeds 800 changed lines. That
is the whole trigger: signal, not calendar. Reviewing unchanged code on a
schedule is waste.

Usage:
    python scripts/arch_check.py                 # gate
    python scripts/arch_check.py --update-baseline
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "dsl41"
BASELINE_PATH = ROOT / "scripts" / "arch_baseline.json"
INDEX_PATH = ROOT / "docs" / "citation-index.md"
TESTS = ROOT / "tests"
#: docs that may cite a test by name. A frozen spec's worked examples are
#: claims, and a claim is only worth its citation.
CITING_DOCS = (ROOT / "docs", ROOT / "CLAUDE.md", ROOT / "README.md")

MAX_MODULE_LINES = 1200
MAX_FUNCTION_LINES = 120
MAX_FUNCTION_BRANCHES = 40
REVIEW_DIFF_LINES = 800
TRIVIAL_BODY_STATEMENTS = 3

#: sha256 of json.dumps(CatalogIR.model_json_schema(), sort_keys=True) at the
#: pinned ir_version. Bumping IR_VERSION is what licenses a new hash here --
#: changing both in one commit is the whole point of the check.
IR_SCHEMA_PIN = {
    "ir_version": "0.2",
    "sha256": "84032aa2eaf2efb08b0c3a4f4342c75fd92fc5b5cf20aa0e1ee3d02629728b2b",
}


class Finding(NamedTuple):
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _body_without_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


# ------------------------------------------------------------ 1. duplicate bodies


def duplicate_function_bodies(paths: Iterable[Path]) -> list[Finding]:
    """Blocking: the same normalised body (docstrings and formatting stripped,
    positions ignored) defined in two different modules. Trivial bodies and
    dunder/protocol stubs are exempt -- `return self._x` is not duplication."""
    seen: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for path in sorted(paths):
        tree = _parse(path)
        if tree is None:
            continue
        for node in _functions(tree):
            if node.name.startswith("__") and node.name.endswith("__"):
                continue
            body = _body_without_docstring(node)
            if len(body) <= TRIVIAL_BODY_STATEMENTS:
                continue
            key = "\n".join(ast.dump(stmt, include_attributes=False) for stmt in body)
            seen[key].append((_rel(path), node.name, node.lineno))
    findings: list[Finding] = []
    for sites in seen.values():
        if len({module for module, _, _ in sites}) < 2:
            continue
        first, rest = sites[0], sites[1:]
        others = ", ".join(f"{module}:{line} {name}()" for module, name, line in rest)
        findings.append(
            Finding(
                first[0],
                first[2],
                f"{first[1]}() has an identical body in another module ({others}):"
                " one owner, or extract the shared helper -- copies drift (DL-72)",
            )
        )
    return sorted(findings)


# --------------------------------------------- 2. private cross-module imports


def _private_import_sites(paths: Iterable[Path]) -> Iterator[tuple[str, int, str, str]]:
    """(module path, line, imported-from module, private name) for every
    `from dsl41.x import _y` and its relative form."""
    for path in sorted(paths):
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if not (node.level or module == "dsl41" or module.startswith("dsl41.")):
                continue
            for alias in node.names:
                if alias.name.startswith("_") and not alias.name.startswith("__"):
                    yield _rel(path), node.lineno, module, alias.name


def private_cross_module_imports(paths: Iterable[Path], allowed: Iterable[str]) -> list[Finding]:
    """Blocking over src/ only (see the module docstring): `from dsl41.x
    import _y`. A private name is a module's own business; importing one
    couples two modules to something neither promised. `allowed` pins the
    sites that predate the gate."""
    known = set(allowed)
    return [
        Finding(
            rel,
            line,
            f"imports the private name {name} from {module}:"
            " make it public where it lives, or move the caller in",
        )
        for rel, line, module, name in _private_import_sites(paths)
        if f"{rel}:{module}.{name}" not in known
    ]


#: DL-83: the watched fields are DERIVED from each state model's own AST, not
#: listed here. A hard-coded list silently stops protecting the moment a field
#: is added -- state_rev is about to be one -- and a gate that quietly narrows
#: is worse than no gate. Still stdlib-only: this reads the source, it does not
#: import dsl41 (that would make a ~1s check depend on the tree it checks).
#:
#: DL-94 widened it from one model to the owner's whole set, for the same
#: reason: a second frozen row arrived (`HostRuntime`, the ss8 routing table)
#: and a gate that protected only the first would have been narrower the day
#: after it was written. DL-120 adds `CapacityReservation`, which rides on a
#: `JobRuntime` and is authoritative for what a live run holds (PR-52).
_STATE_MODELS = ("JobRuntime", "HostRuntime", "CapacityReservation")
_STATE_OWNER = "RuntimeState"
#: Containers on the owner that a caller could reach through instead. Both the
#: private maps and the read-only views over them (DL-86): the views raise at
#: runtime, but a static name is the better error, and a caller who reaches for
#: `_jobs` should be told the same thing as one who reaches for `job`.
#:
#: DL-120 adds the capacity state, `consumed` and `enqueue_counter` (PR-52).
#: The counter is a scalar, not a container -- which is why the gate also
#: watches a plain rebind of these names and not only a subscript write; a
#: number nobody can subscript was reachable by assignment alone. DL-132
#: adds `timer_seq` beside it: the seal carries the timer allocator's
#: high-water mark exactly as it carries the waiter allocator's, and a gate
#: that protected one of the two would be narrower the day after it was
#: written.
_STATE_MAPS = (
    "_jobs",
    "_globals",
    "_timers",
    "_hosts",
    "_consumed",
    "_enqueue_counter",
    "_timer_seq",
    "_period_id",
    "_period_seeded",
    "_inputs_committed",
    "_genesis_finished",
    "job",
    "globals_",
    "hosts",
    "consumed",
    "enqueue_counter",
    "timer_seq",
    "period_id",
)


def _store_aliases(tree: ast.Module) -> set[str]:
    """Names bound to an owner instance in this module: `store = <x>.store`
    (the idiom every runner module uses) and the literal name `store`. A gate
    that matched only the dotted `<x>.store.<name>` form was bypassed by the
    first alias (U1 review); a gate that matched any `<x>._<name>` would flag
    every unrelated class with a private field of the same name, so the alias
    set is derived from the module's own assignments instead."""
    names = {"store"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr == "store":
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return names


def _owner_container(node: ast.expr, aliases: set[str]) -> str | None:
    """The owner container an expression names -- `<...>.store.<name>` or
    `<alias>.<name>` for an alias `_store_aliases` found -- or None."""
    if not isinstance(node, ast.Attribute) or node.attr not in _STATE_MAPS:
        return None
    base = node.value
    if (isinstance(base, ast.Attribute) and base.attr == "store") or (
        isinstance(base, ast.Name) and base.id in aliases
    ):
        return node.attr
    return None


def _model_fields(tree: ast.Module) -> dict[str, str]:
    """Field name -> the state model that declares it, for the models this
    module defines. A name on two models reports the first: the message says
    which field to route through the owner, and the answer is the same either
    way."""
    fields: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in _STATE_MODELS:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.setdefault(stmt.target.id, node.name)
    return fields


def _assigned_attrs(node: ast.AST) -> Iterator[ast.Attribute]:
    """Every attribute reached by an assignment target, unwrapping tuple and
    list destructuring and starred targets -- `a.x, (b.y, *c.z) = ...` writes
    all three."""
    if isinstance(node, ast.Attribute):
        yield node
    elif isinstance(node, ast.Tuple | ast.List):
        for element in node.elts:
            yield from _assigned_attrs(element)
    elif isinstance(node, ast.Starred):
        yield from _assigned_attrs(node.value)


def _owner_class_lines(tree: ast.Module) -> set[int]:
    """Line numbers inside the state-owner class body."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == _STATE_OWNER:
            end = node.end_lineno or node.lineno
            return set(range(node.lineno, end + 1))
    return set()


def state_owner_bypasses(paths: Iterable[Path]) -> list[Finding]:
    """Blocking (DL-82, widened DL-83): a state-model field or a global
    assigned anywhere but the owner. Optimistic locking needs one place where
    "this entity changed" is observable exactly once per applied input, and a
    property test over fed events cannot enforce it -- one feed mutates several
    fields, so a missed site hides behind a sibling's write.

    Covers assignment, augmented assignment, ANNOTATED assignment, `del`,
    tuple/list destructuring, `setattr`, subscript writes through the owner's
    containers, and mapping mutators on them (`store.job.update(...)`).

    Scoped to the module that DEFINES the model plus the container paths any
    other module would go through. It stops accidents, not sabotage -- the same
    stance as the rest of this gate; a determined caller can still alias a row
    out and write it, which is what frozen models are for."""
    findings: list[Finding] = []
    for path in sorted(paths):
        tree = _parse(path)
        if tree is None:
            continue
        fields = _model_fields(tree)
        exempt = _owner_class_lines(tree)
        aliases = _store_aliases(tree)

        def _flag(line: int, message: str, path: Path = path) -> None:
            findings.append(Finding(_rel(path), line, f"{message} (DL-82)"))

        for node in ast.walk(tree):
            if node.lineno in exempt if hasattr(node, "lineno") else False:
                continue
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            elif isinstance(node, ast.Delete):
                targets = list(node.targets)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "setattr"
                and fields
            ):
                _flag(
                    node.lineno,
                    f"setattr() outside {_STATE_OWNER} can reach a state field"
                    " past the assignment gate",
                )
                continue
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                # store.job.update(...) / .setdefault(...) / .pop(...) / .clear()
                container = _owner_container(node.func.value, aliases)
                if container is not None and node.func.attr in (
                    "update",
                    "setdefault",
                    "pop",
                    "clear",
                    "popitem",
                ):
                    _flag(
                        node.lineno,
                        f"mutates store.{container} through .{node.func.attr}():"
                        f" route it through {_STATE_OWNER}",
                    )
                continue
            else:
                continue

            for target in targets:
                for attribute in _assigned_attrs(target):
                    if attribute.attr in fields:
                        _flag(
                            node.lineno,
                            f"assigns {fields[attribute.attr]}.{attribute.attr} directly:"
                            f" route it through {_STATE_OWNER}",
                        )
                    replaced = _owner_container(attribute, aliases)
                    if replaced is not None:
                        _flag(
                            node.lineno,
                            f"rebinds store.{replaced} directly: route it through {_STATE_OWNER}",
                        )
                if isinstance(target, ast.Subscript):
                    written = _owner_container(target.value, aliases)
                    if written is not None:
                        _flag(
                            node.lineno,
                            f"writes store.{written} directly: route it through {_STATE_OWNER}",
                        )
    return findings


def private_import_keys(paths: Iterable[Path]) -> list[str]:
    """Baseline form of the sites above: path:module._name, sorted."""
    return sorted(
        {f"{rel}:{module}.{name}" for rel, _, module, name in _private_import_sites(paths)}
    )


# ------------------------------------------------- 3b. cited tests exist

#: A test named in a doc, in backticks. Literal names only, and the regex is
#: the whole of that rule: a FAMILY (`test_cmNN_*`, `test_semXX_*`,
#: `test_sem09*`) names a convention rather than a function, and every one
#: the docs use carries a `*` or an uppercase placeholder -- neither of which
#: can appear between `test_` and the closing backtick here. No second filter,
#: because a second filter is a second thing to be wrong about.
_CITED_TEST = re.compile(r"`(test_[a-z0-9_]+)`")


def defined_test_names() -> set[str]:
    names: set[str] = set()
    for path in sorted(TESTS.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover -- unreadable test file
            continue
        names |= set(re.findall(r"^\s*def (test_[A-Za-z0-9_]+)", text, re.M))
    return names


def unresolved_test_citations(paths: Iterable[Path]) -> list[Finding]:
    """A doc naming a test that does not exist (DL-110).

    Worked examples in a frozen spec are the document's falsifiable half:
    they say "this is what the code does, and here is what holds it to
    that". A citation that resolves to nothing turns the strongest sentence
    in the document into the least trustworthy one -- and it happens by
    ordinary means, because renaming a test is a refactor nobody thinks of
    as a documentation change.

    Literal names only. The docs also cite FAMILIES (`test_cmNN_*`), which
    name a convention rather than a function."""
    defined = defined_test_names()
    findings: list[Finding] = []
    for path in paths:
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for name in _CITED_TEST.findall(line):
                if name in defined:
                    continue
                findings.append(
                    Finding(_rel(path), index, f"cites `{name}`, which no test defines")
                )
    return findings


def citing_doc_files() -> list[Path]:
    files: list[Path] = []
    for target in CITING_DOCS:
        files += sorted(target.rglob("*.md")) if target.is_dir() else [target]
    return [f for f in files if f.exists()]


# ---------------------------------------------------------------- 3. citations

#: Candidate citation shapes. Uppercase-led because every real namespace is
#: (SEM, UCS, M, L, Q, Qr, ss is handled by the index itself), plus the
#: "word #n" shape that the retired `sol #3` tokens used.
_CANDIDATE = re.compile(
    r"(?<![\w.\-/])(?:[A-Za-z]{1,7})(?:-|\s#)?\d{1,3}[a-z]?(?![\w\-/])",
)
_CAPITALISED_WORD = re.compile(r"[A-Z][a-z]{2,}$")
_PRAGMA = re.compile(r"#\s*(noqa|type|ruff|mypy)\b")
#: Acronyms that look like a namespace and are not one. Keep this short: a
#: token that needs an entry here is a token a reader will misread too.
_NOT_NAMESPACES = frozenset({"UTF", "IST", "ISO", "ASCII"})


def index_patterns(text: str) -> list[re.Pattern[str]]:
    """The regexes in the first column of docs/citation-index.md's namespace
    table. The doc is the source of truth; this only reads it."""
    patterns: list[re.Pattern[str]] = []
    for line in text.splitlines():
        if line.startswith("## Retired namespaces"):
            break
        match = re.match(r"\|\s*`([^`]+)`\s*\|", line)
        if not match:
            continue
        try:
            patterns.append(re.compile(match.group(1).replace(r"\|", "|")))
        except re.error:
            continue
    return patterns


def _prose(path: Path) -> Iterator[tuple[int, str]]:
    """Comments and docstrings -- where citations live. Code is not scanned:
    an identifier is not a citation, and pretending otherwise adds only noise."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT and not _PRAGMA.search(token.string):
                yield token.start[0], token.string
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return
    tree = _parse(path)
    if tree is None:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                yield getattr(node, "lineno", 1), doc


def _is_citation_shaped(token: str, text: str, start: int, end: int) -> bool:
    quoted = text[start - 1 : start] == '"' and text[end : end + 1] == '"'
    if quoted:  # a quoted literal in prose ("W1") names a value, not a source
        return False
    if " #" in token:  # `sol #3`-shaped: a pointer at a conversation
        return True
    if not token[0].isupper():
        return False
    prefix = re.match(r"[A-Za-z]+", token).group(0)  # type: ignore[union-attr]  # always matches
    if _CAPITALISED_WORD.match(prefix):  # Tier-0, Phase-5: prose, not a namespace
        return False
    return prefix not in _NOT_NAMESPACES


def unresolved_citations(
    paths: Iterable[Path], patterns: Iterable[re.Pattern[str]]
) -> list[Finding]:
    """Blocking: a citation-shaped token whose namespace has no row in the
    index. Either the token is a typo, or the namespace is new and the index
    needs the row first."""
    compiled = list(patterns)
    findings: dict[Finding, None] = {}  # one report per (site, token), in order
    for path in sorted(paths):
        for line, text in _prose(path):
            for match in _CANDIDATE.finditer(text):
                token = match.group(0)
                if any(pattern.fullmatch(token) for pattern in compiled):
                    continue
                if not _is_citation_shaped(token, text, match.start(), match.end()):
                    continue
                findings[
                    Finding(
                        _rel(path),
                        line,
                        f"citation {token!r} resolves to no namespace in"
                        " docs/citation-index.md -- add the row, or inline the reason"
                        " and drop the token",
                    )
                ] = None
    return list(findings)


# ------------------------------------------------------------- 4. IR schema pin


def ir_schema_findings(pin: dict[str, str]) -> list[Finding]:
    """Blocking: the IR-F shape moved while IR_VERSION stood still. Anything
    that has read a persisted IR-F would then be silently wrong about it."""
    import hashlib

    sys.path.insert(0, str(ROOT / "src"))
    try:
        from dsl41.ir import IR_VERSION, CatalogIR
    except ImportError as exc:  # not installed: say so, do not pretend to pass
        return [Finding("scripts/arch_check.py", 0, f"cannot import dsl41.ir: {exc}")]
    schema = json.dumps(CatalogIR.model_json_schema(), sort_keys=True)
    digest = hashlib.sha256(schema.encode("utf-8")).hexdigest()
    if digest == pin["sha256"]:
        return []
    if IR_VERSION != pin["ir_version"]:
        print(
            f"note: IR_VERSION moved {pin['ir_version']} -> {IR_VERSION};"
            f" re-pin arch_check.IR_SCHEMA_PIN to {digest}"
        )
        return []
    return [
        Finding(
            "src/dsl41/ir.py",
            0,
            f"the CatalogIR JSON schema changed ({digest}) while IR_VERSION stayed"
            f" {IR_VERSION}: bump IR_VERSION and re-pin arch_check.IR_SCHEMA_PIN",
        )
    ]


# --------------------------------------------------------------- 5. size advisory

_BRANCH_NODES = (
    ast.If,
    ast.IfExp,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.Assert,
    ast.match_case,
)


def branch_count(node: ast.AST) -> int:
    """Decision points: each if/for/while/except/assert/case, each extra
    operand of an and/or, and each comprehension clause and its filters."""
    total = 0
    for child in ast.walk(node):
        if isinstance(child, _BRANCH_NODES):
            total += 1
        elif isinstance(child, ast.BoolOp):
            total += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            total += 1 + len(child.ifs)
    return total


def measure_sizes(paths: Iterable[Path]) -> dict[str, dict[str, int]]:
    """Everything currently over a threshold, as {kind: {subject: value}}."""
    modules: dict[str, int] = {}
    long_functions: dict[str, int] = {}
    branchy: dict[str, int] = {}
    for path in sorted(paths):
        rel = _rel(path)
        try:
            lines = len(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue
        if lines > MAX_MODULE_LINES:
            modules[rel] = lines
        tree = _parse(path)
        if tree is None:
            continue
        for node in _functions(tree):
            subject = f"{rel}:{node.name}"
            span = (node.end_lineno or node.lineno) - node.lineno + 1
            if span > MAX_FUNCTION_LINES:
                long_functions[subject] = span
            branches = branch_count(node)
            if branches > MAX_FUNCTION_BRANCHES:
                branchy[subject] = branches
    return {
        "long_modules": modules,
        "long_functions": long_functions,
        "branchy_functions": branchy,
    }


_SIZE_LABELS = {
    "long_modules": ("lines", MAX_MODULE_LINES),
    "long_functions": ("lines", MAX_FUNCTION_LINES),
    "branchy_functions": ("branches", MAX_FUNCTION_BRANCHES),
}


def size_advisories(measured: dict[str, dict[str, int]], baseline: dict[str, object]) -> list[str]:
    """Advisory: report a size only when it is new, or worse than the day the
    baseline was taken. A ratchet, not a bill for the past."""
    notes: list[str] = []
    for kind, (unit, limit) in _SIZE_LABELS.items():
        recorded = baseline.get(kind) or {}
        previous = recorded if isinstance(recorded, dict) else {}
        for subject, value in sorted(measured[kind].items()):
            was = previous.get(subject)
            if was is None:
                notes.append(f"{subject}: {value} {unit} (over {limit}, new)")
            elif value > int(was):
                notes.append(f"{subject}: {value} {unit} (over {limit}, was {was})")
    return notes


# ------------------------------------------------------------------ escalation


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def changed_lines_since_review() -> tuple[int, str] | None:
    """(changed lines, what we counted from) since the most recent
    arch-review/<date> tag, or the branch point when there is no such tag."""
    tags = _git("tag", "--list", "arch-review/*", "--sort=-creatordate")
    ref = tags.splitlines()[0] if tags else None
    if ref is None:
        ref = _git("merge-base", "HEAD", "main")
    if not ref:
        return None
    stat = _git("diff", "--shortstat", ref, "HEAD")
    if stat is None:
        return None
    changed = sum(int(n) for n in re.findall(r"(\d+) (?:insertion|deletion)", stat))
    return changed, ref


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite scripts/arch_baseline.json from the current tree",
    )
    args = parser.parse_args(argv)

    src_files = sorted(SRC.glob("*.py"))

    if args.update_baseline:
        payload = {
            "note": (
                "Generated by scripts/arch_check.py --update-baseline (DL-75)."
                " private_cross_module_imports pins the src/ sites that predate the"
                " gate: a new one fails the build. The size maps make the advisory"
                " checks a ratchet -- only new or worsened entries are reported."
            ),
            "private_cross_module_imports": private_import_keys(src_files),
            **measure_sizes(src_files),
        }
        BASELINE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"wrote {_rel(BASELINE_PATH)}")
        return 0

    try:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        baseline = {}
    allowed = baseline.get("private_cross_module_imports") or []

    blocking: list[Finding] = []
    blocking += duplicate_function_bodies(src_files)
    blocking += private_cross_module_imports(src_files, allowed)
    try:
        patterns = index_patterns(INDEX_PATH.read_text(encoding="utf-8"))
    except OSError:
        patterns = []
        blocking.append(Finding(_rel(INDEX_PATH), 0, "citation index is missing"))
    if patterns:
        blocking += unresolved_citations(src_files, patterns)
    blocking += unresolved_test_citations(citing_doc_files())
    blocking += ir_schema_findings(IR_SCHEMA_PIN)
    blocking += state_owner_bypasses(src_files)

    advisories = size_advisories(measure_sizes(src_files), baseline)

    for finding in blocking:
        print(f"BLOCK {finding.render()}")
    for note in advisories:
        print(f"note  {note}")
    if not blocking and not advisories:
        print("arch_check: clean")

    reasons: list[str] = []
    if blocking:
        reasons.append(f"{len(blocking)} blocking finding(s)")
    if advisories:
        reasons.append(f"{len(advisories)} advisory finding(s)")
    drift = changed_lines_since_review()
    if drift and drift[0] > REVIEW_DIFF_LINES:
        reasons.append(f"{drift[0]} lines changed since {drift[1]}")
    if reasons:
        print(f"architecture review due -- run /arch-review ({'; '.join(reasons)})")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
