"""A start appends no record before its history row is committed (DL-553), through the real
CLI. A start that cannot commit the row fails as one that cannot append a record fails: the
same exit status, nothing appended, one diagnostic line naming the step and then the error's
own traceback. A restart that commits the row and dies before any record leaves a history
that holds no seq.
"""

from __future__ import annotations

import re
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from restart_sweep import refusal
from test_archive import _take, _view
from test_genesis_eligibility import invocation, running
from test_restore_drill import _cli

from dsl41.runner_store import SqliteStore
from dsl41.runner_transitions import agrees_through

pytestmark = pytest.mark.usefixtures("cli_env")

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

#: `dsl41` with one store method replaced: `fail` raises the store's error, `die` kills the
#: process where the method is entered.
CHILD = """
import os, signal, sqlite3, sys
from dsl41.runner_store import SqliteStore

def fail(*_args, **_kwargs):
    raise sqlite3.OperationalError("disk I/O error")

def die(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)

setattr(SqliteStore, sys.argv[1], {"fail": fail, "die": die}[sys.argv[2]])
from dsl41.cli import app
sys.argv = ["dsl41", *sys.argv[3:]]
app()
"""

#: The line a start that cannot begin its history writes, after the store's path.
LINE = (
    "begin the history: OperationalError: disk I/O error; the start appends no record and"
    " fails (DL-553)"
)


def _child(args: list[str], method: str, effect: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", CHILD, method, effect, *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _journal(root: Path) -> list[tuple[Any, ...]]:
    """Every journal row, every column, as stored."""
    with closing(sqlite3.connect(f"file:{root / 'engine.db'}?mode=ro", uri=True)) as db:
        return db.execute("SELECT * FROM journal ORDER BY seq").fetchall()


def _err(result: subprocess.CompletedProcess[str]) -> str:
    """A child's standard error with ANSI stripped: a traceback is styled where the terminal is
    forced, as under GitHub Actions."""
    return ANSI.sub("", result.stderr)


def _histories(root: Path) -> list[tuple[str, int]]:
    with closing(SqliteStore(root / "engine.db", read_only=True)) as store:
        return store.histories()


#: The one other line a restart prints before its history step, on Linux only: the engine
#: names its signalling primitive when it is built (`runner_stops.EngineStops`, DL-436).
COOKIE_SCAN = "dsl41: cookie scan signalling: "


def _named(result: subprocess.CompletedProcess[str], root: Path) -> None:
    """Standard error holds the one diagnostic line naming the step and the reason, once and
    before the traceback of the store's own error, as an append error at the start propagates
    (DL-553). Before the traceback only the cookie scan's line may stand beside it, and no
    other `dsl41: ERROR:` line is anywhere."""
    diagnostic = f"dsl41: ERROR: {root / 'engine.db'}: {LINE}"
    stderr = _err(result)
    lines = stderr.splitlines()
    traceback = next(
        at for at, line in enumerate(lines) if line.startswith(("╭", "Traceback"))
    )  # rich's, or Python's
    assert stderr.count(diagnostic) == 1, stderr
    assert [line for line in lines[:traceback] if not line.startswith(COOKIE_SCAN)] == [
        diagnostic
    ], stderr
    assert [line for line in lines if line.startswith("dsl41: ERROR:")] == [diagnostic]
    assert lines[-1].endswith("OperationalError: disk I/O error"), stderr


def test_dl553_a_first_start_that_cannot_begin_its_history_fails_as_one_that_cannot_append(
    short_root: Path,
) -> None:
    """Two fresh roots: on one the history row fails, on the other the first append. Both exit
    1 with the store's error last, neither holds a record, and the next start of each meets
    DL-504's refusal, exit 2."""
    started: dict[str, tuple[list[str], Path, subprocess.CompletedProcess[str]]] = {}
    for method in ("begin_history", "append"):
        base = short_root / method
        base.mkdir()
        args = invocation(base, "legacy")
        started[method] = (args, base / "run", _child(args, method, "fail"))
    _, root, failed = started["begin_history"]
    appended = started["append"][2]
    assert failed.returncode == appended.returncode == 1, failed.stderr + appended.stderr
    last = {_err(result).splitlines()[-1] for result in (failed, appended)}
    assert len(last) == 1 and last.pop().endswith("OperationalError: disk I/O error")
    _named(failed, root)
    for args, root, _ in started.values():
        assert _journal(root) == []
        refused = _cli(*args)
        assert refused.returncode == 2, refused.stdout + refused.stderr
        assert refusal(root / "engine.db") in " ".join(_err(refused).split())


def test_dl553_a_restart_that_cannot_begin_its_history_appends_nothing_and_exits_as_an_append(
    short_root: Path,
) -> None:
    """A restart whose history row fails leaves every journal row and history as it was and
    exits as a restart whose first append fails; the next start runs, its history beginning
    after the last record and holding its own."""
    args, root = invocation(short_root, "legacy"), short_root / "run"
    with running(args, root, short_root / "first.log"):
        pass
    journal, histories = _journal(root), _histories(root)
    last = journal[-1][0]
    failed = _child(args, "begin_history", "fail")
    assert (_journal(root), _histories(root)) == (journal, histories)
    _named(failed, root)
    appended = _child(args, "append", "fail")
    assert failed.returncode == appended.returncode == 1, failed.stderr + appended.stderr
    with running(args, root, short_root / "next.log"):
        pass
    assert _histories(root)[-1][1] == last + 1
    assert _journal(root)[-1][0] > last


def test_dl553_a_start_that_dies_after_its_history_row_leaves_a_history_holding_no_seq(
    short_root: Path,
) -> None:
    """A restart commits its history row and is killed at its first append. The next start's
    history begins at the same seq and owns it: the dead one agrees through the seq before,
    as the history before it does, and the archive's line gives it no range."""
    args, root = invocation(short_root, "legacy"), short_root / "run"
    with running(args, root, short_root / "first.log"):
        pass
    last = _journal(root)[-1][0]
    [(first, _)] = _histories(root)
    died = _child(args, "append", "die")
    assert died.returncode == -signal.SIGKILL, died.stderr
    assert _journal(root)[-1][0] == last
    [_, (dead, begun)] = _histories(root)
    assert begun == last + 1
    with running(args, root, short_root / "next.log"):
        pass
    upto = _journal(root)[-1][0]
    histories = _histories(root)
    [_, _, (owner, owned)] = histories
    assert histories[:2] == [(first, 1), (dead, last + 1)] and owned == last + 1
    assert agrees_through(histories, dead, upto) == agrees_through(histories, first, upto) == last
    assert agrees_through(histories, owner, upto) == upto
    archive = short_root / "archive.db"
    _take(root / "engine.db", archive)
    assert _view(archive, "SELECT history, first_seq, last_seq FROM line_ranges ORDER BY 2") == [
        (first, 1, last),
        (owner, last + 1, upto),
    ]
