"""The pre-push checks and CI docs gate, scripts/prepush_checks.py.

Each case builds a tiny git repository under tmp_path and runs the script in
it as a subprocess with `--dry-run` or `--classify`, so no gate command runs.
The index check is called in process: running it through the CLI would run
ruff and mypy first.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "prepush_checks.py"
RENDER = REPO_ROOT / "scripts" / "render_decision_index.py"


def _load() -> ModuleType:
    """scripts/ is not a package, so the test loads the script by path."""
    spec = importlib.util.spec_from_file_location("prepush_checks", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["prepush_checks"] = module
    spec.loader.exec_module(module)
    return module


prepush = _load()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one commit tagged `base`."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _git(tmp_path, "config", "tag.gpgsign", "false")
    _write(tmp_path, "README.md", "# Project\n")
    _write(tmp_path, "docs/guide.md", "# Guide\n")
    _write(tmp_path, "src/pkg/core.py", "X = 1\n")
    _write(tmp_path, "tests/test_decision_log.py", "")
    _write(tmp_path, "tests/test_cards.py", 'CARDS = "docs/blocks/"\n')
    _write(tmp_path, "tests/test_unrelated.py", 'PATH = "src/pkg/core.py"\n')
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "tag", "base")
    return tmp_path


def _run(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "base", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_docs_only_change_runs_the_docs_gate_only(repo: Path) -> None:
    _write(repo, "docs/guide.md", "# Guide\n\nMore.\n")
    _write(repo, "examples/demo/README.md", "# Demo\n")
    _git(repo, "add", "examples")
    _git(repo, "commit", "-q", "-m", "docs")
    _write(repo, "README.md", "# Project, edited\n")
    result = _run(repo, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "docs-only" in result.stdout
    assert "ruff" not in result.stdout and "mypy" not in result.stdout
    assert "pytest" in result.stdout and "arch_check.py" in result.stdout
    assert _run(repo, "--classify").stdout.strip() == "code=false"


def test_code_change_runs_every_gate(repo: Path) -> None:
    _write(repo, "docs/guide.md", "# Guide\n\nMore.\n")
    _write(repo, "notes.txt", "untracked\n")
    result = _run(repo, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert ": code" in result.stdout
    lines = result.stdout.splitlines()
    order = [
        next(i for i, ln in enumerate(lines) if needle in ln)
        for needle in ("ruff check", "ruff format", "mypy", "decision index check", "pytest")
    ]
    assert order == sorted(order)
    assert _run(repo, "--classify").stdout.strip() == "code=true"


def test_a_code_file_moved_under_docs_counts_as_code(repo: Path) -> None:
    _git(repo, "mv", "src/pkg/core.py", "docs/core.md")
    _git(repo, "commit", "-q", "-m", "move")
    assert _run(repo, "--classify").stdout.strip() == "code=true"


def test_docs_flag_skips_the_code_gates(repo: Path) -> None:
    _write(repo, "src/pkg/core.py", "X = 2\n")
    result = _run(repo, "--docs", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "ruff" not in result.stdout and "decision index check" not in result.stdout


def test_a_glob_reader_of_the_changed_directory_is_selected(repo: Path) -> None:
    _write(repo, "docs/blocks/new-card.md", "# Card\n")
    result = _run(repo, "--dry-run")
    pytest_line = next(ln for ln in result.stdout.splitlines() if "pytest" in ln)
    assert "tests/test_cards.py" in pytest_line
    assert "tests/test_decision_log.py" in pytest_line
    assert "tests/test_unrelated.py" not in pytest_line


def test_a_test_naming_no_changed_doc_is_not_selected(repo: Path) -> None:
    _write(repo, "README.md", "# Project, edited\n")
    result = _run(repo, "--dry-run")
    pytest_line = next(ln for ln in result.stdout.splitlines() if "pytest" in ln)
    assert "tests/test_cards.py" not in pytest_line


def test_needles_skip_a_readme_basename() -> None:
    assert prepush.needles("docs/blocks/README.md") == {
        "docs/blocks/README.md",
        "docs/blocks/",
        '"docs"',
        "'docs'",
        '"blocks"',
        "'blocks'",
    }
    assert "guide.md" in prepush.needles("docs/guide.md")


def test_a_top_level_folder_name_selects_no_test(repo: Path) -> None:
    _write(repo, "tests/test_mentions_docs.py", "# reads nothing under docs/\n")
    _write(repo, "docs/guide.md", "# Guide\n\nMore.\n")
    _write(repo, "examples/demo/notes.md", "# Notes\n")
    result = _run(repo, "--dry-run")
    pytest_line = next(ln for ln in result.stdout.splitlines() if "pytest" in ln)
    assert "tests/test_mentions_docs.py" not in pytest_line
    assert "tests/test_decision_log.py" in pytest_line
    assert "arch_check.py" in result.stdout


def _pytest_line(repo: Path) -> str:
    return next(ln for ln in _run(repo, "--dry-run").stdout.splitlines() if "pytest" in ln)


def test_a_reader_building_the_path_from_quoted_parts_is_selected(repo: Path) -> None:
    _write(repo, "tests/test_parts.py", 'CARDS = ROOT / "docs" / "blocks"\n')
    _write(repo, "docs/blocks/x.md", "# X\n")
    assert "tests/test_parts.py" in _pytest_line(repo)


def test_a_reader_globbing_the_top_level_folder_is_selected(repo: Path) -> None:
    _write(repo, "tests/test_single.py", "FILES = sorted((ROOT / 'docs').glob('*.md'))\n")
    _write(repo, "docs/guide.md", "# Guide\n\nMore.\n")
    assert "tests/test_single.py" in _pytest_line(repo)


def test_a_bare_folder_in_a_comment_selects_nothing(repo: Path) -> None:
    _write(repo, "tests/test_comment.py", "# the files under docs/ are not read here\n")
    _write(repo, "docs/blocks/x.md", "# X\n")
    assert "tests/test_comment.py" not in _pytest_line(repo)


def test_needles_quote_each_component_in_both_styles() -> None:
    found = prepush.needles("docs/blocks/x.md")
    assert {'"docs"', "'docs'", '"blocks"', "'blocks'"} <= found
    assert "docs/" not in found


def test_an_empty_change_set_is_code(repo: Path) -> None:
    assert prepush.is_docs_only([]) is False
    assert _run(repo, "--classify").stdout.strip() == "code=true"


def test_an_unresolvable_base_is_refused(repo: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "no-such-ref", "--dry-run"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "does not resolve" in result.stderr


def test_missing_git_is_refused(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    env = dict(os.environ, PATH=str(tmp_path_factory.mktemp("empty-path")))
    result = _run(repo, "--dry-run", env=env)
    assert result.returncode == 2
    assert "git is not available" in result.stderr


def _index_repo(repo: Path) -> Path:
    (repo / "scripts").mkdir()
    shutil.copy(RENDER, repo / "scripts" / "render_decision_index.py")
    _write(repo, "docs/decision-log.md", "- DL-01 First decision.\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "log")
    return repo


def test_a_current_index_passes(repo: Path) -> None:
    _index_repo(repo)
    subprocess.run(
        [sys.executable, str(repo / "scripts" / "render_decision_index.py")],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert not prepush.index_stale(repo)


def test_a_stale_index_is_detected(repo: Path) -> None:
    _index_repo(repo)
    _write(repo, "docs/decision-index.md", "# Decision index\n")
    assert prepush.index_stale(repo)
