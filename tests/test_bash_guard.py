"""scripts/hooks/bash_guard.sh decides on a Bash tool input before the command runs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parent.parent / "scripts" / "hooks" / "bash_guard.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="the guard needs jq")


def decide(command: str, cwd: Path | None = None) -> tuple[str | None, str]:
    """The permission decision and its reason; (None, "") when the guard stays silent."""
    payload: dict[str, object] = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    done = subprocess.run(
        ["sh", str(GUARD)], input=json.dumps(payload), capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    if not done.stdout.strip():
        return None, ""
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    return out["permissionDecision"], out["permissionDecisionReason"]


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def conflicted(tmp_path: Path) -> Path:
    """A repo in the middle of a merge whose conflict is still unresolved."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "switch", "-q", "-c", "other")
    (repo / "a.txt").write_text("other\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "other")
    git(repo, "switch", "-q", "main")
    (repo / "a.txt").write_text("main\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "main")
    merge = subprocess.run(
        ["git", "merge", "other"], cwd=repo, capture_output=True, text=True, check=False
    )
    assert merge.returncode != 0
    return repo


@pytest.mark.parametrize(
    "command",
    [
        "git restore a.py",
        "git checkout main",
        "git -C /x checkout -- a.py",
        "git clean -fd",
        "git reset --hard HEAD~1",
        "git add -A",
        "git add --all",
        "git add .",
        "cd x && git add . && echo done",
        "git push --force origin b",
        "git push origin b -f",
        "git pull",
        "FOO=1 git pull --rebase",
    ],
)
def test_denied(command: str) -> None:
    decision, reason = decide(command)
    assert decision == "deny" and reason.endswith(".")


@pytest.mark.parametrize(
    "command",
    [
        "git branch -D old",
        "git worktree remove --force ../w",
        "git push origin --delete old",
        "git push origin :old",
        "gh pr merge 12 --rebase --delete-branch",
    ],
)
def test_asked(command: str) -> None:
    decision, reason = decide(command)
    assert decision == "ask" and reason


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git push --force-with-lease --force-if-includes origin b",
        "git push --force --force-with-lease origin b",
        "git add src/a.py",
        'git commit -m "do not git checkout or git add -A"',
        "git branch -d merged",
        "gh pr merge 12 --rebase",
        "echo hi",
    ],
)
def test_allowed(command: str) -> None:
    assert decide(command) == (None, "")


def test_other_tools_are_ignored() -> None:
    payload = {"tool_name": "Read", "tool_input": {"command": "git pull"}}
    done = subprocess.run(
        ["sh", str(GUARD)], input=json.dumps(payload), capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0 and done.stdout == ""


@pytest.mark.parametrize(
    "command", ["git commit -m x", "git rebase --continue", "git cherry-pick --continue"]
)
def test_unresolved_conflict_blocks_the_next_step(conflicted: Path, command: str) -> None:
    decision, reason = decide(command, conflicted)
    assert decision == "deny" and "a.txt" in reason and "conflict marker" in reason


def test_resolved_conflict_lets_the_commit_go_on(conflicted: Path) -> None:
    (conflicted / "a.txt").write_text("both\n", encoding="utf-8")
    assert decide("git commit -m x", conflicted) == (None, "")


def test_add_of_a_marked_path_is_denied(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "bad.md").write_text("text\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> other\n")
    (repo / "good.md").write_text("text\n========\n======= not a marker\n", encoding="utf-8")
    decision, reason = decide("git add bad.md", repo)
    assert decision == "deny" and "bad.md" in reason
    assert decide("git add good.md", repo) == (None, "")
    decision, reason = decide(f"cd {repo} && git add good.md bad.md")
    assert decision == "deny" and "bad.md" in reason
