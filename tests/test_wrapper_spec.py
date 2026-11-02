"""O: standalone input refusals, pinned to frozen wrapper-protocol §2 and §4.6."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dsl41 import runner_wrapper

RUN_ID = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unknown", "x"),
        ("run_id", "not-a-uuid"),
        ("run_id", RUN_ID.upper().replace("000000000001", "00000000000A")),
        ("run_id", RUN_ID.replace("-4000-", "-1000-")),
        ("run_id", RUN_ID.replace("-8000-", "-7000-")),
        ("run_id", RUN_ID + "\n"),
        ("job", ""),
        ("job", "."),
        ("job", ".."),
        ("job", "../outside"),
        ("job", "a/b"),
        ("run_number", True),
        ("run_number", 1.0),
        ("env", {"X": 1}),
        ("env", {"X": "a\0b"}),
        ("env", {"X\0Y": "a"}),
        *[
            (key, "a\0b")
            for key in (
                "run_id",
                "job",
                "command",
                "run_dir",
                "stdout_path",
                "stderr_path",
                "stdin_path",
            )
        ],
        *[
            (key, 42)
            for key in (
                "run_id",
                "job",
                "command",
                "run_dir",
                "stdout_path",
                "stderr_path",
                "stdin_path",
            )
        ],
    ],
)
def test_frozen_spec_refuses_before_any_effect(tmp_path: Path, field: str, value: object) -> None:
    """O, wrapper-protocol §2: exit 2 names the field; no log, spool or command effect."""
    read_fd, write_fd = os.pipe()
    spec = {
        "version": 1,
        "run_id": RUN_ID,
        "job": "synthetic",
        "run_number": 1,
        "command": f"touch {tmp_path / 'effect'}",
        "run_dir": str(tmp_path),
        "lifeline_fd": read_fd,
        "stdout_path": str(tmp_path / "out"),
        "stderr_path": str(tmp_path / "err"),
        "stdin_path": None,
        "grace_seconds": 0,
    }
    spec[field] = value
    try:
        result = subprocess.run(
            [sys.executable, runner_wrapper.__file__],
            input=json.dumps(spec).encode(),
            pass_fds=(read_fd,),
            capture_output=True,
            timeout=10,
            check=False,
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert result.returncode == 2, result.stderr
    assert field.encode() in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_frozen_spec_accepts_component_and_optional_defaults() -> None:
    """O, wrapper-protocol §2/§4.6: valid strings pass; stdin and grace retain defaults."""
    assert (
        runner_wrapper._spec_refusal(
            {
                "version": 1,
                "run_id": RUN_ID,
                "job": "synthetic.name-1",
                "run_number": 1,
                "command": "true",
                "run_dir": "/synthetic",
                "lifeline_fd": 3,
                "stdout_path": "/synthetic/out",
                "stderr_path": "/synthetic/err",
                "env": {"X": ""},
            }
        )
        is None
    )


@pytest.mark.parametrize("value", [None, [], "text", 1, True])
def test_non_object_spec_refuses(tmp_path: Path, value: object) -> None:
    """O, wrapper-protocol §2: one object is required; the refusal precedes effects."""
    assert runner_wrapper._spec_refusal(value) == "spec is not an object"
    result = subprocess.run(
        [sys.executable, "-I", runner_wrapper.__file__],
        input=json.dumps(value).encode(),
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2 and b"spec is not an object" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_alternate_separator_is_not_a_job_component(monkeypatch: pytest.MonkeyPatch) -> None:
    """O, wrapper-protocol §2: an alternate separator also splits directory components."""
    monkeypatch.setattr(runner_wrapper.os, "altsep", "\\")
    assert "job" in runner_wrapper._spec_refusal({"version": 1, "job": "a\\b"})


def test_malformed_json_keeps_exit_one(tmp_path: Path) -> None:
    """O, DL-444: malformed JSON exits 1, distinct from readable non-object refusal."""
    result = subprocess.run(
        [sys.executable, "-I", runner_wrapper.__file__],
        input=b"{bad",
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1 and list(tmp_path.iterdir()) == []
