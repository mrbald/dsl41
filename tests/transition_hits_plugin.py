"""Pytest plugin: record state-machine transition hits for the whole session.

At session start it makes a temp directory and sets `DSL41_TRANSITION_HITS` to
it and `DSL41_TRANSITION_STRICT=1`, so subprocesses inherit both. At session
finish it merges every per-pid file into `.transition-hits.json` at the
repository root, and fails the session when a violation was recorded.
`scripts/transition_coverage.py` reads that file.

A test that triggers a violation on purpose points `DSL41_TRANSITION_HITS` at
its own `tmp_path` with `monkeypatch`, so the violation stays out of the session.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from dsl41.state_machine import HITS_ENV, STRICT_ENV

ROOT = Path(__file__).resolve().parent.parent
HITS_FILE = ROOT / ".transition-hits.json"

_directory: str | None = None


def _read_lines(directory: Path, pattern: str, *, fail_closed: bool) -> list[dict[str, str]]:
    """Every JSON record of the matching files.

    An undecodable line is skipped, except with `fail_closed`: a violation torn by a
    kill still counts as a violation, with a record that names the file.
    """
    records: list[dict[str, str]] = []
    for path in sorted(directory.glob(pattern)):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                record = None
            if isinstance(record, dict):
                records.append(record)
            elif fail_closed:
                records.append(
                    {
                        "machine": "?",
                        "id": "?",
                        "old": "",
                        "new": "",
                        "reason": f"unparsable line in {path.name}",
                    }
                )
    return records


def _unique_sorted(records: list[dict[str, str]]) -> list[dict[str, str]]:
    unique = {json.dumps(r, sort_keys=True): r for r in records}
    return [unique[key] for key in sorted(unique)]


def merge(directory: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """The hits and the violations of every process, deduplicated and sorted."""
    return (
        _unique_sorted(_read_lines(directory, "hits-*.jsonl", fail_closed=False)),
        _unique_sorted(_read_lines(directory, "violations-*.jsonl", fail_closed=True)),
    )


def write_report(path: Path, hits: list[dict[str, str]], violations: list[dict[str, str]]) -> None:
    text = json.dumps({"hits": hits, "violations": violations}, indent=1, sort_keys=True)
    # a temp file in the same directory, then a rename: a reader never sees half a file
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".transition-hits-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text + "\n")
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def pytest_sessionstart(session: pytest.Session) -> None:
    global _directory
    # a file left by a killed run must never satisfy the gate
    HITS_FILE.unlink(missing_ok=True)
    _directory = tempfile.mkdtemp(prefix="dsl41-hits-")
    os.environ[HITS_ENV] = _directory
    os.environ[STRICT_ENV] = "1"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _directory
    directory, _directory = _directory, None
    os.environ.pop(HITS_ENV, None)
    os.environ.pop(STRICT_ENV, None)
    if directory is None:
        return
    hits, violations = merge(Path(directory))
    shutil.rmtree(directory, ignore_errors=True)
    write_report(HITS_FILE, hits, violations)
    if violations:
        print(f"\nstate-machine violations recorded: {len(violations)}", file=sys.stderr)
        for v in violations:
            print(
                f"  {v['machine']} {v['id']}: {v['old']} -> {v['new']}: {v['reason']}",
                file=sys.stderr,
            )
        if session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
