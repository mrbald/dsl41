"""scripts/hooks/bash_guard.sh decides on a Bash tool input before the command runs."""

from __future__ import annotations

import json
import os
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
        # git options that hold "=" do not hide the command
        "git -c user.name=x push --force origin b",
        "git clean --exclude=foo -fdx",
        # wrappers and an attached -C
        "env git pull",
        "env -u VIRTUAL_ENV git reset --hard",
        "env -i A=1 git pull",
        "command git pull",
        "exec git pull",
        "nice -n 5 git pull",
        "caffeinate -i git pull",
        "sudo -u me git pull",
        "git -C. checkout x",
        "git -Csub reset --hard",
        "echo $(git pull)",
        # quotes around a flag do not hide it
        'git add "-A"',
        "git add '--all'",
        'git reset "--hard"',
        # forced pushes: --force and a + refspec both override a lease
        "git push origin +b",
        "git push origin +HEAD:main",
        "git push --force-with-lease origin +b",
        "git push --force --force-with-lease origin b",
        "git push --no-force-with-lease --force origin b",
        "git push origin b:--force-with-lease --force",
        # other ways to discard work or stage everything
        "git switch --discard-changes main",
        "git switch -f main",
        "git restore --staged --worktree a.py",
        "git restore -SW a.py",
        "git add :/",
        "git add *",
        "git stash drop",
        "git stash clear",
        "timeout 60 git push -f origin b",
        "xargs -n 1 git pull",
        # a continued line is one command
        "git push \\\n  --force origin b",
        # a heredoc ends, and the next command is read
        "cat > f.sh <<'EOF'\ngit status\nEOF\ngit pull",
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
        'git branch "-D" old',
        "git branch --delete --force old",
        "git branch -d -f old",
        "git branch -df old",
        "git -C. branch -D old",
        'gh pr merge 12 "--delete-branch"',
        "gh pr merge 5 -d --rebase",
        "gh -R o/r pr merge 5 --delete-branch",
        "gh --repo=o/r pr merge 5 --delete-branch",
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
        "git push --force-with-lease=b origin b",
        "git add src/a.py",
        'git commit -m "do not git checkout or git add -A"',
        'git commit -m "first line\ngit checkout x\ngit add -A"',
        "git commit -F - <<'EOF'\ngit checkout x\ngit pull\nEOF",
        'git commit -F - <<"EOF"\ngit reset --hard\nEOF',
        "cat > f.sh <<EOF\ngit pull\nEOF",
        "cat > f.sh <<-EOF\n\tgit pull\n\tEOF\necho done",
        "git branch -d merged",
        "git branch -f b HEAD",
        "gh pr merge 12 --rebase",
        "gh -R o/r pr merge 12 --rebase",
        "echo hi",
        "echo git pull",
        "git clean -n",
        "git clean -nd",
        "git clean --dry-run -fdx",
        "git restore --staged a.py",
        "git restore -S a.py",
        "git switch main",
        "git switch -c new",
        "git -c core.editor=true commit --author=a -m x",
        "env -u VIRTUAL_ENV uv run pytest",
        "git status 2>&1 | tail -3",
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
    "command",
    [
        "git commit -m x",
        "git rebase --continue",
        "git cherry-pick --continue",
        "git commit --author=a -m x",
        "git -c core.editor=true rebase --continue",
        "env -u VIRTUAL_ENV git commit -m x",
    ],
)
def test_unresolved_conflict_blocks_the_next_step(conflicted: Path, command: str) -> None:
    decision, reason = decide(command, conflicted)
    assert decision == "deny" and "a.txt" in reason and "conflict marker" in reason, (
        explain_silence(command, conflicted)
    )


def explain_silence(command: str, repo: Path) -> str:
    """What the guard saw: the trace of a run, the repository state, the file."""
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    trace = subprocess.run(
        ["sh", "-x", str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=False
    )
    git_env = {k: v for k, v in os.environ.items() if k.startswith("GIT")}
    return (
        f"stdout: {trace.stdout}\ntrace: {trace.stderr[-3000:]}\n"
        f"status: {status.stdout}{status.stderr}\n"
        f"a.txt: {(repo / 'a.txt').read_text(encoding='utf-8')!r}\nenv: {git_env}"
    )


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
