"""scripts/worktree.sh refuses a bad call before it touches git."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "worktree.sh"


def run(*args: str, path: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": path} if path is not None else None
    return subprocess.run(
        ["/bin/sh", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30
    )


def test_worktree_refuses_without_wt_on_the_path() -> None:
    done = run("some-branch", path="/usr/bin:/bin")
    assert done.returncode == 2
    assert done.stderr.strip() == "worktree.sh: wt is not on PATH."


def test_worktree_refuses_a_missing_branch() -> None:
    done = run(path="/usr/bin:/bin")
    assert done.returncode == 64
    assert done.stderr.startswith("usage: scripts/worktree.sh BRANCH")
    # the synced .venv is ignored, so removal needs the flag
    assert "wt remove --discard-ignored" in done.stderr
