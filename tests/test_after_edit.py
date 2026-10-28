"""scripts/hooks/after_edit.sh checks an edited file in the checkout that holds it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "scripts" / "hooks" / "after_edit.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("uv") is None,
    reason="the hook needs jq and uv",
)


def run_hook(file_path: str, project_dir: Path) -> str:
    """The additional context the hook reports; "" when it stays silent."""
    payload = {"tool_name": "Edit", "tool_input": {"file_path": file_path}}
    # The project dir names another place, so the checkout must come from the file.
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project_dir)}
    done = subprocess.run(
        ["sh", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    if not done.stdout.strip():
        return ""
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    context = out["additionalContext"]
    assert isinstance(context, str)
    return context


@pytest.fixture
def scratch() -> Iterator[Path]:
    """A Python file under tests/ that pytest does not collect; removed afterwards."""
    path = ROOT / "tests" / f"_after_edit_scratch_{os.getpid()}.py"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def test_a_bad_python_file_is_reported(scratch: Path, tmp_path: Path) -> None:
    scratch.write_text("import os\n", encoding="utf-8")
    assert "ruff check" in run_hook(str(scratch), tmp_path)


def test_a_path_with_a_dot_segment_is_normalised(scratch: Path, tmp_path: Path) -> None:
    scratch.write_text("import os\n", encoding="utf-8")
    dotted = f"{ROOT}/./tests/{scratch.name}"
    assert "ruff check" in run_hook(dotted, tmp_path)


def test_a_clean_python_file_is_not_reported(scratch: Path, tmp_path: Path) -> None:
    scratch.write_text('"""Nothing to report."""\n', encoding="utf-8")
    assert run_hook(str(scratch), tmp_path) == ""


def test_a_file_outside_any_repo_is_not_checked(tmp_path: Path) -> None:
    outside = tmp_path / "src" / "bad.py"
    outside.parent.mkdir()
    outside.write_text("import os\n", encoding="utf-8")
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(outside)}}
    done = subprocess.run(
        ["sh", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(ROOT),
            "GIT_CEILING_DIRECTORIES": str(tmp_path),
        },
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
