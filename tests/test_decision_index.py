"""The generated decision index, scripts/render_decision_index.py."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "render_decision_index.py"
INDEX = REPO_ROOT / "docs" / "decision-index.md"
LOG = REPO_ROOT / "docs" / "decision-log.md"


def _load() -> ModuleType:
    """scripts/ is not a package, so the test loads the script by path."""
    spec = importlib.util.spec_from_file_location("render_decision_index", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_decision_index"] = module
    spec.loader.exec_module(module)
    return module


rdi = _load()


def test_the_committed_index_is_the_rendering() -> None:
    """Breaks when the log or a citing doc changed and nobody ran
    `uv run python scripts/render_decision_index.py`."""
    assert rdi.is_current(REPO_ROOT), (
        "docs/decision-index.md is stale; run `uv run python scripts/render_decision_index.py`"
    )


def _tree(root: Path) -> None:
    (root / "docs").mkdir()
    (root / "docs" / "decision-log.md").write_text("- DL-01 One\n  body\n", encoding="utf-8")
    (root / "docs" / "a.md").write_text("cites DL-01\n", encoding="utf-8")
    (root / "README.md").write_text("none\n", encoding="utf-8")


def _git_add(root: Path, *names: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", *names], cwd=root, check=True)


def test_a_matching_index_is_current(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "docs" / "decision-index.md").write_text(
        rdi.render_index(tmp_path), encoding="utf-8"
    )
    assert rdi.is_current(tmp_path)


def test_a_new_citation_makes_the_index_stale(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "docs" / "decision-index.md").write_text(
        rdi.render_index(tmp_path), encoding="utf-8"
    )
    (tmp_path / "docs" / "b.md").write_text("also DL-01\n", encoding="utf-8")
    assert not rdi.is_current(tmp_path)


def test_a_missing_index_is_not_current(tmp_path: Path) -> None:
    _tree(tmp_path)
    assert not rdi.is_current(tmp_path)


def test_only_tracked_docs_count(tmp_path: Path) -> None:
    """An untracked scratch doc never puts its name into the public index."""
    _tree(tmp_path)
    (tmp_path / "docs" / "scratch.md").write_text("DL-01\n", encoding="utf-8")
    (tmp_path / "docs" / "staged.md").write_text("DL-01\n", encoding="utf-8")
    _git_add(tmp_path, "README.md", "docs/decision-log.md", "docs/a.md", "docs/staged.md")
    row = rdi.render_index(tmp_path).splitlines()[-1]
    assert "a.md" in row and "staged.md" in row and "scratch.md" not in row


def test_a_tree_inside_another_repo_is_walked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    inner = tmp_path / "inner"
    inner.mkdir()
    _tree(inner)
    row = rdi.render_index(inner).splitlines()[-1]
    assert "a.md" in row
    assert "scanning every doc" in capsys.readouterr().err


def test_the_entry_count_equals_the_header_count() -> None:
    headers = re.findall(r"^- DL-\d+[a-z]? ", LOG.read_text(encoding="utf-8"), re.M)
    entries = rdi.parse_entries(LOG.read_text(encoding="utf-8"))
    assert len(entries) == len(headers) == len({i for i, _ in entries})


def test_suffixed_ids_are_entries() -> None:
    entries = dict(rdi.parse_entries("- DL-41 Base\n  body\n- DL-41a Amend | x\n"))
    assert list(entries) == ["DL-41", "DL-41a"]
    assert entries["DL-41a"] == "Amend | x"


def test_a_wrapped_title_is_joined() -> None:
    log = "- DL-1 First half of a\n  title that wraps. Body.\n\n- DL-2 Next\n"
    assert rdi.parse_entries(log) == [
        ("DL-1", "First half of a title that wraps"),
        ("DL-2", "Next"),
    ]


def test_a_date_parenthetical_ends_the_title() -> None:
    assert rdi.make_title("Drill runs as user (2026-10-04; PR 77). More.") == "Drill runs as user"


def test_a_plain_parenthetical_is_kept() -> None:
    title = rdi.make_title("New repo (round-trip + equivalence) instead of X. Body.")
    assert title == "New repo (round-trip + equivalence) instead of X"


def test_a_long_title_is_cut_at_a_word() -> None:
    title = rdi.make_title("word " * 60)
    assert title.endswith("...") and len(title) <= 163
    assert rdi.make_title("x" * 155 + " abcd efgh") == "x" * 155 + " abcd..."
    assert rdi.make_title("x" * 154 + " abcde fgh") == "x" * 154 + " abcde..."
    assert title[:-3].split() == ["word"] * len(title[:-3].split())


def test_indented_mentions_are_not_headers() -> None:
    entries = rdi.parse_entries("- DL-1 One\n  - DL-2 not a header\nDL-3 plain\n")
    assert [i for i, _ in entries] == ["DL-1"]


def test_whole_ids_only() -> None:
    found = rdi.cited_ids("see DL-4, DL-41a/DL-42 and DL-263..DL-271; not DL-410 or XDL-5")
    assert found == {"DL-4", "DL-41a", "DL-42", "DL-263", "DL-271", "DL-410"}
    assert "DL-4" not in rdi.cited_ids("DL-41 and DL-41a and DL-4x")


def test_abbreviations_and_versions_do_not_end_a_sentence() -> None:
    assert rdi.make_title("Use it, e.g. now, i.e. later. More") == "Use it, e.g. now, i.e. later"
    assert rdi.make_title("See cf. DL-1 and a vs. b. More") == "See cf. DL-1 and a vs. b"
    assert rdi.make_title("Python 3.12 wins. More") == "Python 3.12 wins"


def test_a_hyphen_before_an_id_still_cites() -> None:
    assert rdi.cited_ids("pre-DL-7 and XDL-5") == {"DL-7"}


def test_pipes_in_titles_are_escaped() -> None:
    row = rdi.render([("DL-1", "a | b")], {}).splitlines()[-1]
    assert row == "| DL-1 | a \\| b |  |"


def test_cited_by_links_are_relative_to_docs() -> None:
    row = rdi.render([("DL-1", "t")], {"DL-1": ["README.md", "docs/a.md"]}).splitlines()[-1]
    assert row == "| DL-1 | t | [README.md](../README.md), [docs/a.md](a.md) |"
