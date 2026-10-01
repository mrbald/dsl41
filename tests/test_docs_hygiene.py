"""Merge-conflict markers never reach the documentation (DL-237).

A scripted rebase once left a diff3 `|||||||` line in the decision log. Git
writes each marker as exactly seven characters at the start of a line; the
three labelled markers are followed by a space and a label. A setext heading
underline or a longer rule of the same character is not a marker.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = re.compile(r"^(?:(?:<{7}|>{7}|\|{7})(?: |$)|={7}$)")


def documents(root: Path) -> list[Path]:
    """The Markdown the repository publishes or instructs agents with: docs/,
    examples/, .claude/skills/, the README, CLAUDE.md and AGENTS.md."""
    found = [
        *root.glob("docs/**/*.md"),
        *root.glob("examples/**/*.md"),
        *root.glob(".claude/skills/**/*.md"),
    ]
    singles = [root / name for name in ("README.md", "CLAUDE.md", "AGENTS.md")]
    return sorted([*found, *(path for path in singles if path.exists())])


def conflict_markers(paths: list[Path]) -> list[str]:
    """`path:line: text` for every line that is a merge-conflict marker."""
    return [
        f"{path}:{number}: {line}"
        for path in paths
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if MARKER.match(line)
    ]


def test_docs_have_no_conflict_markers() -> None:
    paths = documents(ROOT)
    assert ROOT / "docs" / "decision-log.md" in paths
    assert ROOT / "README.md" in paths
    assert conflict_markers(paths) == []


def test_conflict_marker_lines_are_reported(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    page = tmp_path / "docs" / "page.md"
    page.write_text(
        "intro\n"
        "<<<<<<< HEAD\n"
        "ours\n"
        "||||||| parent of d178f78\n"
        "base\n"
        "=======\n"
        "theirs\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )
    assert conflict_markers(documents(tmp_path)) == [
        f"{page}:2: <<<<<<< HEAD",
        f"{page}:4: ||||||| parent of d178f78",
        f"{page}:6: =======",
        f"{page}:8: >>>>>>> branch",
    ]


def test_headings_rules_and_inline_markers_are_not_reported(tmp_path: Path) -> None:
    (tmp_path / "examples" / "demo").mkdir(parents=True)
    (tmp_path / "README.md").write_text(
        "Title\n========\n\nShort\n===\n\n<<<<<<<< eight\n>>>>>>>> eight\n", encoding="utf-8"
    )
    (tmp_path / "examples" / "demo" / "README.md").write_text(
        " =======\n=======x\n||||||\nsee `<<<<<<< HEAD` in text\n<<<<<<<x\n", encoding="utf-8"
    )
    assert len(documents(tmp_path)) == 2
    assert conflict_markers(documents(tmp_path)) == []
