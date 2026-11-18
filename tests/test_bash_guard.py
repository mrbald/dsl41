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
    # git checks the committer identity before a merge touches the tree, so the
    # merge carries the same identity as the commits.
    merge = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "merge", "other"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert merge.returncode == 1, merge.stderr
    assert (repo / "a.txt").read_text(encoding="utf-8").startswith("<<<<<<< ")
    return repo


ASK_DELETE = "This push deletes a remote branch."


@pytest.fixture
def remote_clone(tmp_path: Path) -> Path:
    """A clone, fetched, whose remote has main, two merged branches and an unmerged one."""
    bare = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    bare.mkdir()
    git(bare, "init", "-q", "--bare", "-b", "main")
    clone.mkdir()
    git(clone, "init", "-q", "-b", "main")
    git(clone, "remote", "add", "origin", str(bare))
    (clone / "a.txt").write_text("base\n", encoding="utf-8")
    git(clone, "add", "a.txt")
    git(clone, "commit", "-q", "-m", "base")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "switch", "-q", "-c", "merged")
    (clone / "a.txt").write_text("merged\n", encoding="utf-8")
    git(clone, "commit", "-q", "-am", "merged")
    git(clone, "push", "-q", "origin", "merged")
    git(clone, "switch", "-q", "main")
    git(clone, "merge", "-q", "--ff-only", "merged")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "push", "-q", "origin", "main:merged2")
    git(clone, "switch", "-q", "-c", "unmerged")
    (clone / "b.txt").write_text("unmerged\n", encoding="utf-8")
    git(clone, "add", "b.txt")
    git(clone, "commit", "-q", "-m", "unmerged")
    git(clone, "push", "-q", "origin", "unmerged")
    git(clone, "switch", "-q", "main")
    git(clone, "fetch", "-q", "origin")
    return clone


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


@pytest.mark.parametrize(
    "command",
    [
        "git push origin --delete merged",
        "git push -q origin --delete merged",
        "git push --quiet origin -d merged",
        "git push --delete origin merged",
        "git push origin --delete merged merged2",
        "git push origin --delete refs/heads/merged",
        "  git push origin --delete merged  ",
    ],
)
def test_the_exact_delete_of_merged_branches_is_silent(remote_clone: Path, command: str) -> None:
    assert decide(command, remote_clone) == (None, "")


def test_the_exact_delete_names_its_directory(remote_clone: Path, tmp_path: Path) -> None:
    command = f"git -C {remote_clone} push -q origin --delete merged"
    assert decide(command, tmp_path) == (None, "")
    assert decide("git -C clone push origin --delete merged", tmp_path) == (None, "")
    assert decide(f"git -C {tmp_path / 'nosuch'} push origin --delete merged", tmp_path) == (
        "ask",
        ASK_DELETE,
    )


@pytest.mark.parametrize(
    "command",
    [
        "git push origin --delete unmerged",
        "git push origin --delete merged unmerged",
        "git push origin --delete nosuch",
        "git push origin --delete main",
        "git push origin :main",
        "git push origin :merged",
        "git push origin --delete HEAD",
        "git push origin :HEAD",
        "git push origin --delete refs/tags/v1",
        "git push origin --delete refs/heads/main",
        "git push origin --delete 'mer*'",
        "git push origin --delete",
        "git push upstream --delete merged",
        "git push --no-verify origin --delete merged",
        "git push origin --delete merged -o x",
        "git push origin --delete merged --force-with-lease",
        # a delete written inside a longer line, or with anything else on it
        "cd {clone} && git push origin --delete merged",
        "git push origin --delete merged; echo done",
        "git push origin --delete merged && echo done",
        "echo done; git push origin --delete merged",
        "env X=1 git push origin --delete merged",
        "env git push origin --delete merged",
        "git push origin --delete merged | cat",
        "git push origin --delete merged > /dev/null",
        "git push origin --delete merged # note",
        "git push origin --delete $B",
        "git push origin --delete $(echo merged)",
        "git push origin --delete `echo merged`",
        "git push origin --delete 'merged'",
        'git push origin --delete "merged"',
        "git push origin --delete merged\ngit push origin --delete unmerged",
        "git push origin --delete merged\ngit push origin --delete merged2",
        "git push origin --delete merged;git push origin --delete unmerged",
        "git push origin --delete merged && git push origin --delete unmerged",
        "git push origin --delete unmerged && git push origin --delete merged",
        "git push origin --delete merged && git branch -D x",
        "git branch -D x; git push origin --delete merged",
        # F2, F3, C3, C4: the command changes what git reads
        "echo unmerged | xargs git push origin --delete merged",
        "git push origin --delete merged | xargs echo",
        "git --git-dir=/elsewhere/.git push origin --delete merged",
        "git --work-tree=/elsewhere push origin --delete merged",
        "git --config-env=remote.origin.url=X push origin --delete merged",
        "git --namespace=x push origin --delete merged",
        "GIT_DIR=/elsewhere/.git git push origin --delete merged",
        "GIT_WORK_TREE=/elsewhere git push origin --delete merged",
        "GIT_CONFIG_COUNT=1 git push origin --delete merged",
        "GIT_COMMON_DIR=/elsewhere git push origin --delete merged",
        "GIT_NAMESPACE=x git push origin --delete merged",
        "HOME=/elsewhere git push origin --delete merged",
        "git -c remote.origin.url=/elsewhere push origin --delete merged",
        "git -c remote.origin.pushurl=/elsewhere push origin --delete merged",
        "env -C /elsewhere git push origin --delete merged",
        "env --chdir=/elsewhere git push origin --delete merged",
        "cd -P /elsewhere && git push origin --delete merged",
        "cd -- /elsewhere && git push origin --delete merged",
        "W=/elsewhere; cd $W && git push origin --delete merged",
        "pushd /elsewhere && git push origin --delete merged",
        "sudo -D /elsewhere git push origin --delete merged",
        # P1, P2, C1: options that delete, in full or shortened
        "git push origin --d merged",
        "git push origin --de merged",
        "git push origin --del merged",
        "git push origin --dele merged",
        "git push origin --delet merged",
        "git push origin --de unmerged",
        "git push --prune origin 'refs/heads/*:refs/heads/*'",
        "git push --pru origin",
        "git push --prun origin",
        "git push --prune origin",
        "git push --mirror origin",
        "git push --m origin",
        "git push --mi origin",
        "git push --mir origin",
        "git push --mirr origin",
        "git push --mirro origin",
    ],
)
def test_any_other_delete_asks(remote_clone: Path, command: str) -> None:
    command = command.replace("{clone}", str(remote_clone))
    decision, reason = decide(command, remote_clone)
    assert decision == "ask" and reason


def test_a_delete_outside_a_repository_asks(tmp_path: Path) -> None:
    assert decide("git push origin --delete merged", tmp_path) == ("ask", ASK_DELETE)


def test_a_delete_without_the_remote_tracking_ref_asks(remote_clone: Path) -> None:
    git(remote_clone, "update-ref", "-d", "refs/remotes/origin/merged")
    assert decide("git push origin --delete merged", remote_clone) == ("ask", ASK_DELETE)


def test_a_forced_delete_is_denied(remote_clone: Path) -> None:
    decision, reason = decide("git push --force origin --delete merged", remote_clone)
    assert decision == "deny" and reason.endswith(".")


def other_bare(repo: Path) -> Path:
    """A second bare repository next to the clone, to stand for a fork."""
    other = repo.parent / "other.git"
    other.mkdir()
    git(other, "init", "-q", "--bare", "-b", "main")
    return other


def test_a_remote_with_another_push_url_asks(remote_clone: Path) -> None:
    git(remote_clone, "remote", "set-url", "--push", "origin", str(other_bare(remote_clone)))
    assert decide("git push origin --delete merged", remote_clone) == ("ask", ASK_DELETE)


def test_a_remote_with_a_push_instead_of_asks(remote_clone: Path) -> None:
    other = other_bare(remote_clone)
    git(
        remote_clone,
        "config",
        f"url.{other}.pushInsteadOf",
        str(remote_clone.parent / "remote.git"),
    )
    assert decide("git push origin --delete merged", remote_clone) == ("ask", ASK_DELETE)


def test_a_remote_with_two_urls_asks(remote_clone: Path) -> None:
    other = other_bare(remote_clone)
    git(remote_clone, "remote", "set-url", "--add", "origin", str(other))
    assert decide("git push origin --delete merged", remote_clone) == ("ask", ASK_DELETE)


def test_a_remote_with_two_push_urls_asks(remote_clone: Path) -> None:
    bare = remote_clone.parent / "remote.git"
    git(remote_clone, "remote", "set-url", "--push", "--add", "origin", str(bare))
    git(
        remote_clone,
        "remote",
        "set-url",
        "--push",
        "--add",
        "origin",
        str(other_bare(remote_clone)),
    )
    assert decide("git push origin --delete merged", remote_clone) == ("ask", ASK_DELETE)


def test_a_name_that_is_also_a_local_tag_asks(remote_clone: Path) -> None:
    git(remote_clone, "tag", "-m", "t", "merged", "main")
    assert decide("git push origin --delete merged", remote_clone) == ("ask", ASK_DELETE)


def test_the_default_branch_of_the_remote_is_the_base(remote_clone: Path) -> None:
    git(remote_clone, "push", "-q", "origin", "unmerged:trunk")
    git(remote_clone, "fetch", "-q", "origin")
    git(remote_clone, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert decide("git push origin --delete unmerged", remote_clone) == (None, "")
    assert decide("git push origin --delete merged", remote_clone) == (None, "")
    assert decide("git push origin --delete trunk", remote_clone) == ("ask", ASK_DELETE)


def test_without_a_remote_head_the_default_branch_is_main(remote_clone: Path) -> None:
    git(remote_clone, "update-ref", "-d", "--no-deref", "refs/remotes/origin/HEAD")
    assert decide("git push origin --delete merged", remote_clone) == (None, "")
    assert decide("git push -q origin --delete merged", remote_clone) == (None, "")
    assert decide("git push origin --delete unmerged", remote_clone) == ("ask", ASK_DELETE)
    assert decide("git push origin --delete main", remote_clone) == ("ask", ASK_DELETE)


@pytest.mark.parametrize("name", ["Merged", "MERGED", "merGed"])
def test_a_branch_of_another_case_is_not_proven(remote_clone: Path, name: str) -> None:
    assert decide("git push origin --delete merged", remote_clone) == (None, "")
    assert decide(f"git push origin --delete {name}", remote_clone) == ("ask", ASK_DELETE)
