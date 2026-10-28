#!/usr/bin/env python3
"""Local pre-push checks, and the docs gate CI runs for every change.

A change is docs-only when every changed path is `README.md`, a Markdown
file under `docs/`, or a Markdown file under `examples/`. The changed paths
are `git diff BASE...HEAD` plus the working tree: staged, unstaged and
untracked files that `git status` lists.

    uv run python scripts/prepush_checks.py [--base REF]     # pre-push
    uv run python scripts/prepush_checks.py --docs [--base REF]
    python3 scripts/prepush_checks.py --classify --base REF  # CI: code=true|false

Without `--docs`, a docs-only change runs the docs gate only. Any other
change runs ruff, ruff format, mypy and the decision-index freshness check,
then the docs gate. The docs gate runs the doc tests, the tests that name a
changed Markdown file, and the architecture gate. Each command prints with
its exit status; the first failure stops the run with exit 1. Exit 2 means
git is missing or the base does not resolve.

As a git pre-push hook, git passes the remote name and URL; they are ignored.
Stdlib only, like scripts/arch_check.py.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_BASE = "origin/main"

#: Always in the docs gate: the log, its index, links and hygiene read every doc.
DOC_TESTS = (
    "tests/test_decision_log.py",
    "tests/test_decision_index.py",
    "tests/test_docs_links.py",
    "tests/test_docs_hygiene.py",
)

CODE_GATES = (
    ("uv", "run", "ruff", "check", "src", "tests", "examples"),
    ("uv", "run", "ruff", "format", "--check", "src", "tests", "scripts", "examples"),
    # DL-193: tests/uc_oracle.py is held to the type gate by name.
    ("uv", "run", "mypy", "src", "tests/uc_oracle.py"),
)

#: DL-75: the architecture gate checks that docs name existing tests and modules,
#: so a docs-only change can fail it.
ARCH_GATE = ("uv", "run", "python", "scripts/arch_check.py")

STALE_INDEX = (
    "decision index stale: run uv run python scripts/render_decision_index.py and stage it"
)


class Refusal(Exception):
    """git is missing or the base does not resolve (exit 2)."""


def git(root: Path | None, *args: str) -> str:
    """stdout of a git command; Refusal when git cannot run, CalledProcessError on failure."""
    cmd = ["git", *(("-C", str(root)) if root else ()), *args]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise Refusal("git is not available") from exc
    return out.stdout


def repo_root() -> Path:
    if shutil.which("git") is None:
        raise Refusal("git is not available")
    try:
        return Path(git(None, "rev-parse", "--show-toplevel").strip())
    except subprocess.CalledProcessError as exc:
        raise Refusal("not inside a git repository") from exc


def resolve(root: Path, base: str) -> str:
    try:
        return git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").strip()
    except subprocess.CalledProcessError as exc:
        raise Refusal(f"base ref {base!r} does not resolve") from exc


def changed_paths(root: Path, base: str) -> list[str]:
    """Paths changed since the merge base with `base`, and in the working tree."""
    resolve(root, base)
    # --no-renames lists both sides, so a code file moved under docs/ still counts as code
    committed = git(root, "diff", "--name-only", "--no-renames", "-z", f"{base}...HEAD")
    paths = {p for p in committed.split("\0") if p}
    status = git(root, "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all")
    paths.update(entry[3:] for entry in status.split("\0") if len(entry) > 3)
    return sorted(paths)


def is_docs_path(path: str) -> bool:
    if path == "README.md":
        return True
    return path.endswith(".md") and path.startswith(("docs/", "examples/"))


def is_docs_only(paths: list[str]) -> bool:
    """An empty change set is not docs-only: nothing proves the code is untouched."""
    return bool(paths) and all(is_docs_path(p) for p in paths)


def needles(path: str) -> set[str]:
    """Strings whose presence in a test file marks it as a reader of `path`."""
    found = {path}
    parts = path.split("/")
    # A top-level folder is named in most test files' comments, so only deeper
    # folders mark a glob reader; DOC_TESTS and ARCH_GATE cover the whole tree.
    found.update("/".join(parts[:i]) + "/" for i in range(2, len(parts)))
    if parts[-1] != "README.md":
        found.add(parts[-1])
    # A reader that builds the path from parts (`ROOT / "docs" / "blocks"`) names
    # each component quoted. The top-level folder counts only when quoted, so a
    # bare `docs/` in a comment still selects nothing.
    if len(parts) > 1:
        for name in parts if parts[-1] != "README.md" else parts[:-1]:
            found.update((f'"{name}"', f"'{name}'"))
    return found


def doc_test_files(root: Path, paths: list[str]) -> list[str]:
    """DOC_TESTS plus every tests/test_*.py that names a changed Markdown path."""
    wanted: set[str] = set()
    for path in paths:
        if path.endswith(".md"):
            wanted |= needles(path)
    selected = list(DOC_TESTS)
    for test in sorted((root / "tests").glob("test_*.py")):
        rel = test.relative_to(root).as_posix()
        if rel in selected:
            continue
        text = test.read_text(encoding="utf-8", errors="replace")
        if any(n in text for n in wanted):
            selected.append(rel)
    return [t for t in selected if (root / t).is_file()]


def index_stale(root: Path) -> bool:
    """True when the committed docs/decision-index.md differs from a fresh rendering."""
    script = root / "scripts" / "render_decision_index.py"
    spec = importlib.util.spec_from_file_location("_prepush_render_index", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return not module.is_current(root)


def run(root: Path, cmd: tuple[str, ...]) -> int:
    print("$ " + " ".join(cmd), flush=True)
    try:
        status = subprocess.run(cmd, cwd=root, check=False).returncode
    except FileNotFoundError:
        status = 127
    print(f"exit {status}", flush=True)
    return status


def pytest_cmd(tests: list[str]) -> tuple[str, ...]:
    # DL-354: four workers, one test file each at a time, as the CI legs run.
    return ("uv", "run", "pytest", "-q", "-n", "4", "--dist", "loadfile", *tests)


def plan(root: Path, paths: list[str], docs: bool) -> list[tuple[str, ...]]:
    """The steps in order; ("index",) stands for the decision-index check."""
    steps: list[tuple[str, ...]] = []
    if not docs:
        steps += [*CODE_GATES, ("index",)]
    steps += [pytest_cmd(doc_test_files(root, paths)), ARCH_GATE]
    return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docs", action="store_true", help="run the docs gate only")
    parser.add_argument("--base", default=DEFAULT_BASE, help="base ref (default origin/main)")
    parser.add_argument("--classify", action="store_true", help="print code=true|false")
    parser.add_argument("--dry-run", action="store_true", help="print the steps, run nothing")
    parser.add_argument("hook_args", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        root = repo_root()
        paths = changed_paths(root, args.base)
    except Refusal as exc:
        print(f"prepush_checks: {exc}", file=sys.stderr)
        return 2
    docs_only = is_docs_only(paths)
    if args.classify:
        print(f"code={'false' if docs_only else 'true'}")
        return 0
    print(f"{len(paths)} changed path(s) since {args.base}: {'docs-only' if docs_only else 'code'}")
    for step in plan(root, paths, args.docs or docs_only):
        if args.dry_run:
            print("$ " + ("decision index check" if step == ("index",) else " ".join(step)))
            continue
        if step == ("index",):
            stale = index_stale(root)
            print("decision index: " + ("stale" if stale else "current"), flush=True)
            if stale:
                print(STALE_INDEX, file=sys.stderr)
                return 1
            continue
        if run(root, step) != 0:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
