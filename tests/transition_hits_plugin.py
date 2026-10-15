"""Pytest plugin: record statechart coverage hits for the whole session.

At configure time it makes a temp directory and sets `DSL41_TRANSITION_HITS`
to it, so subprocesses inherit it. At session finish it merges every per-pid
file into `.transition-hits.json` at the repository root.
`scripts/transition_coverage.py` reads that file.

Under pytest-xdist the controller owns the directory and the report. Its
`pytest_configure` runs before xdist starts the workers in its session start,
so every worker inherits the directory and writes its per-pid files there.
A worker does nothing in either hook: if it did, each worker would remove the
report and the controller, which runs no tests, would write it last and empty.

A test whose hits must stay out of the session, such as one that builds cells
by hand, points `DSL41_TRANSITION_HITS` elsewhere or unsets it with
`monkeypatch`.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from dsl41.state_machine import HITS_ENV

ROOT = Path(__file__).resolve().parent.parent
HITS_FILE = ROOT / ".transition-hits.json"

_directory: str | None = None


def merge(directory: Path) -> list[dict[str, str]]:
    """The hits of every process, deduplicated and sorted. An undecodable
    line, a write torn by a kill, is skipped."""
    records: list[dict[str, str]] = []
    for path in sorted(directory.glob("hits-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    unique = {json.dumps(r, sort_keys=True): r for r in records}
    return [unique[key] for key in sorted(unique)]


def write_report(path: Path, hits: list[dict[str, str]]) -> None:
    text = json.dumps({"hits": hits}, indent=1, sort_keys=True)
    # a temp file in the same directory, then a rename: a reader never sees half a file
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".transition-hits-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text + "\n")
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _is_worker(config: pytest.Config) -> bool:
    """True in a pytest-xdist worker, which has `workerinput`; the controller
    and a serial run do not."""
    return getattr(config, "workerinput", None) is not None


def pytest_configure(config: pytest.Config) -> None:
    global _directory
    if _is_worker(config):
        return
    # a file left by a killed run must never satisfy the gate
    HITS_FILE.unlink(missing_ok=True)
    _directory = tempfile.mkdtemp(prefix="dsl41-hits-")
    os.environ[HITS_ENV] = _directory


def pytest_unconfigure(config: pytest.Config) -> None:
    """Undo a configure that no session finished: `pytest --help` and
    `--markers` configure without a session, and a session that failed to
    start never finishes. The directory goes and the env is cleared."""
    global _directory
    if _is_worker(config) or _directory is None:
        return
    directory, _directory = _directory, None
    os.environ.pop(HITS_ENV, None)
    shutil.rmtree(directory, ignore_errors=True)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _directory
    if _is_worker(session.config):
        return
    directory, _directory = _directory, None
    os.environ.pop(HITS_ENV, None)
    if directory is None:
        return
    hits = merge(Path(directory))
    shutil.rmtree(directory, ignore_errors=True)
    write_report(HITS_FILE, hits)
