"""The consumer finder for rewritten Markdown, scripts/doc_deps.py.

Each case builds a tiny git repository under tmp_path, rewrites a document
after the `base` tag, and runs the script in it as a subprocess.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "doc_deps.py"

GUIDE = """# Guide

## Release steps

<!-- steps:start -->
The release verb reads the status file before it applies anything.

| Step | Owner |
| --- | --- |
| apply | operator |
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository at version 1.2.0 whose guide is cited by a test and the log."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _git(tmp_path, "config", "tag.gpgsign", "false")
    _write(tmp_path, "pyproject.toml", '[project]\nname = "demo"\nversion = "1.2.0"\n')
    _write(tmp_path, "docs/guide.md", GUIDE)
    _write(tmp_path, "docs/decision-log.md", "- DL-01 see guide.md#release-steps\n")
    _write(tmp_path, "tests/test_guide.py", 'LINK = "guide.md#release-steps"\n')
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "tag", "base")
    return tmp_path


def _run(repo: Path, *paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "base", *paths],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_renamed_heading_reports_its_anchor_consumer(repo: Path) -> None:
    _write(repo, "docs/guide.md", GUIDE.replace("## Release steps", "## Releasing"))
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "#release-steps -> tests/test_guide.py:1" in result.stdout
    assert "decision-log.md" not in result.stdout.split("\n", 1)[1]


def test_a_removed_sentence_and_marker_are_items(repo: Path) -> None:
    sentence = "The release verb reads the status file before it applies anything."
    _write(repo, "README.md", f"{sentence}\n<!-- steps:start -->\n")
    _git(repo, "add", "README.md")
    _write(repo, "docs/guide.md", GUIDE.replace(sentence, "").replace("<!-- steps:start -->", ""))
    result = _run(repo, "docs/guide.md")
    assert result.returncode == 1
    assert f"{sentence} -> README.md:1" in result.stdout
    assert "<!-- steps:start --> -> README.md:2" in result.stdout


def test_a_removed_long_table_header_cell_is_an_item(repo: Path) -> None:
    header = "Person who runs each release step"
    _write(repo, "docs/guide.md", GUIDE.replace("| Step | Owner |", f"| Step | {header} |"))
    _git(repo, "add", "docs/guide.md")
    _git(repo, "commit", "-q", "-m", "long header")
    _git(repo, "tag", "long")
    _write(repo, "docs/other.md", f"Ask the {header} first.\n")
    _git(repo, "add", "docs/other.md")
    _write(repo, "docs/guide.md", GUIDE.replace("| Step | Owner |", "| Step | Who |"))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "long"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert f"{header} -> docs/other.md:1" in result.stdout


def test_a_short_table_header_cell_is_below_the_floor(repo: Path) -> None:
    _write(repo, "docs/other.md", "Ask the Owner first.\n")
    _git(repo, "add", "docs/other.md")
    _write(repo, "docs/guide.md", GUIDE.replace("| Step | Owner |", "| Step | Who |"))
    result = _run(repo)
    assert "Owner -> " not in result.stdout


def test_an_edit_nobody_cites_has_no_hit(repo: Path) -> None:
    _write(repo, "docs/guide.md", GUIDE + "\nA new closing paragraph that nobody quotes.\n")
    result = _run(repo)
    assert result.returncode == 0, result.stdout
    assert "0 hit(s)" in result.stdout


def test_version_tokens_on_added_lines_are_compared(repo: Path) -> None:
    _write(repo, "docs/guide.md", GUIDE + "\nInstall 1.2.0, not 1.1.9.\n")
    result = _run(repo)
    assert "version 1.2.0 matches pyproject" in result.stdout
    assert "version 1.1.9 differs from pyproject (1.2.0)" in result.stdout


def test_an_unresolvable_base_is_refused(repo: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "no-such-ref"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "does not resolve" in result.stderr
