#!/usr/bin/env python3
"""Prove a comment-and-docstring edit changed no code. Stdlib only.

Usage:
    python scripts/comment_equivalence.py <base-rev> [paths...]

With no paths, every file under src/, tests/, scripts/ and examples/ that
`git diff --name-only <base-rev> --` reports, and every untracked one, is
checked. For each .py file the AST, minus docstrings, must equal the base AST,
and no citation token present in the base may be missing from the working
file. A changed docstring on a pydantic model is a finding: pydantic puts it in
the schema. A changed, added or deleted file that is not .py is a finding. A
git failure prints git's message and exits 1.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREES = ("src", "tests", "scripts", "examples")
CITATION_SHAPES = (
    r"DL-\d{1,3}[a-z]?",
    r"SEM-\d{2}[a-z]?",
    r"UCS-\d{1,2}",
    r"M\d{2}",
    r"P-M\d{2}",
    r"R\d",
    r"L\d{3}",
    r"CM-\d{2}",
    r"S\d[a-f]?",
    r"T-\d{3}",
    r"T\d{2}[a-z]?",
    r"D\d",
    r"F[1-4]",
    r"ss\d{1,2}[a-z]?(?:\.\d{1,2})?",
    r"Q\d{1,2}[a-z]?(?:\([ivx]+\))?",
    r"Qr\d",
    r"U\d[a-z]?",
    r"E\d{1,2}",
    r"PR-\d{2}[a-z]?",
    r"PR-Q\d",
    r"[IBC][12]",
    r"[a-z][a-z_]*\.\d{2,3}",
    r"PENDING: (?:Q\d{1,2}[a-z]?|Qr\d|U\d[a-z]?|E\d{1,2})",
)
CITATION_RE = re.compile(rf"\b(?:{'|'.join(CITATION_SHAPES)})(?!\w)")


class GitError(Exception):
    pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)


def _git_lines(root: Path, *args: str) -> list[str]:
    out = _git(root, *args)
    if out.returncode != 0:
        raise GitError(out.stderr.decode("utf-8", errors="replace").strip())
    return out.stdout.decode("utf-8").splitlines()


def changed_paths(root: Path, base: str) -> list[str]:
    tracked = _git_lines(root, "diff", "--name-only", base, "--", *TREES)
    untracked = _git_lines(root, "ls-files", "--others", "--exclude-standard", "--", *TREES)
    return sorted(set(tracked) | set(untracked))


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:]
    return tree


def code_dump(text: str) -> str:
    return ast.dump(_strip_docstrings(ast.parse(text)), include_attributes=False)


def _is_model(node: ast.ClassDef, models: set[str]) -> bool:
    for base in node.bases:
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        if name == "BaseModel" or name in models:
            return True
    return False


def model_docstrings(text: str) -> dict[str, str | None]:
    classes = [n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.ClassDef)]
    models: set[str] = set()
    grown = True
    while grown:
        grown = False
        for node in classes:
            if node.name not in models and _is_model(node, models):
                models.add(node.name)
                grown = True
    return {n.name: ast.get_docstring(n) for n in classes if n.name in models}


def citation_tokens(text: str) -> set[str]:
    return set(CITATION_RE.findall(text))


def _show(root: Path, base: str, rel: str) -> bytes | None:
    shown = _git(root, "show", f"{base}:{rel}")
    return shown.stdout if shown.returncode == 0 else None


def _python_findings(rel: str, base: str, before: str, after: str) -> list[str]:
    findings: list[str] = []
    try:
        if code_dump(before) != code_dump(after):
            findings.append(f"{rel}: code differs from {base} (beyond comments and docstrings)")
        old, new = model_docstrings(before), model_docstrings(after)
        for name in sorted(n for n in old.keys() | new.keys() if old.get(n) != new.get(n)):
            findings.append(f"{rel}: schema-bearing docstring changed on class {name}")
    except SyntaxError as exc:
        findings.append(f"{rel}: does not parse ({exc.msg})")
    lost = sorted(citation_tokens(before) - citation_tokens(after))
    if lost:
        findings.append(f"{rel}: lost citation tokens: {', '.join(lost)}")
    return findings


def check_file(root: Path, base: str, rel: str) -> list[str]:
    before = _show(root, base, rel)
    path = root / rel
    if before is None:
        return [f"{rel}: file added since {base}"]
    if not path.is_file():
        return [f"{rel}: file deleted since {base}"]
    if not rel.endswith(".py"):
        if before != path.read_bytes():
            return [f"{rel}: file changed since {base} (not a .py file)"]
        return []
    return _python_findings(rel, base, before.decode("utf-8"), path.read_text(encoding="utf-8"))


def run(root: Path, base: str, paths: list[str]) -> tuple[list[str], int]:
    _git_lines(root, "rev-parse", "--verify", f"{base}^{{commit}}")
    rels = paths or changed_paths(root, base)
    findings: list[str] = []
    for rel in rels:
        findings += check_file(root, base, rel)
    return findings, len(rels)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 2
    try:
        findings, count = run(ROOT, args[0], args[1:])
    except GitError as exc:
        print(f"comment_equivalence: git failed: {exc}", file=sys.stderr)
        return 1
    if findings:
        print("\n".join(findings))
        return 1
    print(f"comment_equivalence: {count} files equal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
