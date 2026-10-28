"""scripts/peer.sh refuses a bad call before it starts Codex, and keeps the thread id and
the last agent message of the stream it gets back."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PEER = ROOT / "scripts" / "peer.sh"
EXTRACT = ROOT / "scripts" / "peer_extract.py"
GOOD_BRIEF = "Review the diff. Please call no other model.\n"


def stream(*events: dict[str, object]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def message(text: str) -> dict[str, object]:
    return {"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": text}}


def extract(tmp_path: Path, jsonl: str, *extra: str) -> subprocess.CompletedProcess[str]:
    source = tmp_path / "run.jsonl"
    source.write_text(jsonl, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(EXTRACT), str(source), str(tmp_path / "run"), *extra],
        capture_output=True,
        text=True,
        timeout=30,
    )


def peer(
    cwd: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(PEER), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_extract_keeps_the_thread_and_the_last_agent_message(tmp_path: Path) -> None:
    done = extract(
        tmp_path,
        stream(
            {"type": "thread.started", "thread_id": "t-1"},
            message("first"),
            {"type": "item.completed", "item": {"type": "command_execution", "text": "x"}},
            message("second\nline"),
            {"type": "turn.completed"},
        )
        + "not json\n",
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["t-1", str(tmp_path / "run.md")]
    assert (tmp_path / "run.thread").read_text(encoding="utf-8") == "t-1\n"
    assert (tmp_path / "run.md").read_text(encoding="utf-8") == "second\nline\n"


def test_extract_exits_70_without_an_agent_message(tmp_path: Path) -> None:
    done = extract(tmp_path, stream({"type": "thread.started", "thread_id": "t-2"}))
    assert done.returncode == 70
    assert "no agent message" in done.stderr
    assert not (tmp_path / "run.md").exists()
    assert (tmp_path / "run.thread").read_text(encoding="utf-8") == "t-2\n"


def test_extract_takes_the_resumed_thread_when_the_stream_names_none(tmp_path: Path) -> None:
    done = extract(tmp_path, stream(message("again")), "--thread", "t-3")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == "t-3"


def test_peer_refuses_too_few_arguments(tmp_path: Path) -> None:
    done = peer(tmp_path, "m", "brief")
    assert done.returncode == 64 and "usage:" in done.stderr


def test_peer_refuses_a_brief_without_the_no_other_model_line(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("Review the diff.\n", encoding="utf-8")
    done = peer(tmp_path, "m", str(brief), str(tmp_path / "out"), "60")
    assert done.returncode == 64 and "call no other model" in done.stderr


def test_peer_refuses_outside_a_git_repo(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text(GOOD_BRIEF, encoding="utf-8")
    work = tmp_path / "plain"
    work.mkdir()
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)}
    done = peer(work, "m", str(brief), str(tmp_path / "out"), "60", env=env)
    assert done.returncode == 65 and "git repo" in done.stderr


def test_peer_runs_codex_with_the_documented_arguments(tmp_path: Path) -> None:
    """A stub `codex` stands in; the call line and the extraction are checked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "seen"
    stub = bin_dir / "codex"
    stub.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$SEEN"\ncat > /dev/null\n'
        'echo \'{"type":"thread.started","thread_id":"t-9"}\'\n'
        'echo \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    brief = tmp_path / "brief.md"
    brief.write_text(GOOD_BRIEF, encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SEEN": str(seen)}
    out = tmp_path / "out"
    done = peer(repo, "gpt-x", str(brief), str(out), "60", env=env)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["t-9", f"{out}.md"]
    assert seen.read_text(encoding="utf-8").splitlines() == [
        "exec",
        "--sandbox",
        "read-only",
        "--json",
        "-m",
        "gpt-x",
        "-c",
        'model_reasoning_effort="xhigh"',
        "-",
    ]
    resumed = peer(repo, "gpt-x", str(brief), str(out), "60", "--resume", "t-9", env=env)
    assert resumed.returncode == 0, resumed.stderr
    assert seen.read_text(encoding="utf-8").splitlines() == [
        "exec",
        "resume",
        "t-9",
        "--json",
        "-c",
        'model_reasoning_effort="high"',
        "-c",
        'sandbox_mode="read-only"',
        "-",
    ]
