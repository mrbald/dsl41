#!/usr/bin/env python3
"""Find the consumers of text a Markdown change removed or rewrote.

    uv run python scripts/doc_deps.py [--base REF] [PATHS...]

For each Markdown file (default: those changed since the merge base with
REF, committed or in the working tree), the old side of every removed or
changed line yields items: heading text, its GitHub anchor as `#anchor`,
HTML comments, and each sentence or table cell (header cells included)
of 25 or more characters. `git grep -F` finds each item in the other tracked files, the
decision log excepted, and the hits print as `ITEM -> file:line`, grouped by
the file that changed. Version tokens on added lines are then compared with
`[project] version` in pyproject.toml.

Exit 1 when any item has a hit, for the caller to inspect; 0 otherwise;
2 when git is missing or the base does not resolve. Stdlib only.
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from prepush_checks import DEFAULT_BASE, Refusal, changed_paths, git, repo_root, resolve

#: The decision log is append-only history; quoting old text there is not a dependency.
EXCLUDED = ("docs/decision-log.md",)
MIN_SENTENCE = 25

_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_COMMENT = re.compile(r"<!--.*?-->")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_LINE_MARKER = re.compile(r"^\s*(?:[-*+>]|\d+\.)\s+")
_VERSION = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?!\.?\d)")


def anchor(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[^\w\- ]", "", heading.lower())
    return "#" + text.replace(" ", "-")


def table_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|") if c.strip()]


def items_from_line(lines: list[str], index: int) -> set[str]:
    """The searchable items one old line holds."""
    line = lines[index]
    found = set(_COMMENT.findall(line))
    heading = _HEADING.match(line)
    if heading and heading.group(1):
        found |= {heading.group(1), anchor(heading.group(1))}
        return found
    if line.lstrip().startswith("|"):
        cells = table_cells(line)
        found.update(c for c in cells if len(c) >= MIN_SENTENCE)
        return found
    text = _COMMENT.sub("", _LINE_MARKER.sub("", line)).strip()
    found.update(s.strip() for s in _SENTENCE_END.split(text) if len(s.strip()) >= MIN_SENTENCE)
    return found


def old_text(root: Path, base: str, path: str) -> str:
    try:
        return git(root, "show", f"{base}:{path}")
    except subprocess.CalledProcessError:
        return ""


def diff_sides(old: list[str], new: list[str]) -> tuple[list[int], list[str]]:
    """Indexes of removed or changed old lines, and the added or changed new lines."""
    removed: list[int] = []
    added: list[str] = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(range(i1, i2))
        if tag in ("replace", "insert"):
            added.extend(new[j1:j2])
    return removed, added


def grep(root: Path, item: str, path: str) -> list[str]:
    """`file:line` for each tracked line holding `item`, outside `path` and EXCLUDED."""
    excludes = [f":(exclude){p}" for p in (path, *EXCLUDED)]
    try:
        out = git(root, "grep", "-n", "-F", "-e", item, "--", ".", *excludes)
    except subprocess.CalledProcessError:
        return []
    return [":".join(hit.split(":", 2)[:2]) for hit in out.splitlines()]


def project_version(root: Path) -> str | None:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    with pyproject.open("rb") as handle:
        version = tomllib.load(handle).get("project", {}).get("version")
    return str(version) if version else None


def check_file(root: Path, base: str, path: str, version: str | None) -> int:
    """Print the hits and version tokens for one file; return the hit count."""
    old = old_text(root, base, path).splitlines()
    target = root / path
    new = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    removed, added = diff_sides(old, new)
    items: set[str] = set()
    for index in removed:
        items |= items_from_line(old, index)
    hits = [(hit, item) for item in sorted(items) for hit in grep(root, item, path)]
    print(f"== {path}: {len(items)} item(s), {len(hits)} hit(s)")
    for hit, item in sorted(hits, key=lambda pair: _sort_key(pair[0])):
        print(f"{item} -> {hit}")
    tokens = sorted({token for line in added for token in _VERSION.findall(line)})
    for token in tokens:
        if token == version:
            print(f"version {token} matches pyproject")
        else:
            print(f"version {token} differs from pyproject ({version})")
    return len(hits)


def _sort_key(hit: str) -> tuple[str, int]:
    name, _, line = hit.rpartition(":")
    return name, int(line) if line.isdigit() else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=DEFAULT_BASE, help="base ref (default origin/main)")
    parser.add_argument("paths", nargs="*", help="Markdown files (default: changed ones)")
    args = parser.parse_args(argv)
    try:
        root = repo_root()
        resolve(root, args.base)
        # the merge base, so text that changed only on the base branch is not reported
        base = git(root, "merge-base", args.base, "HEAD").strip()
        paths = args.paths or [p for p in changed_paths(root, args.base) if p.endswith(".md")]
    except (Refusal, subprocess.CalledProcessError) as exc:
        print(f"doc_deps: {exc}", file=sys.stderr)
        return 2
    version = project_version(root)
    total = sum(check_file(root, base, path, version) for path in paths)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
