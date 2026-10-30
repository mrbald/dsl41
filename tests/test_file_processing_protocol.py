"""Durable-boundary and cross-role probes of the file workers; effects live under tmp_path.

Child processes stop with os._exit at a failpoint or pause on a stdin handshake.
No probe sleeps or depends on timing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from file_processing_checker import check_counts_outputs, record

from dsl41 import file_processing as fp
from dsl41.file_processing_charts import PROCESS
from examples.file_processing.worker import EXIT_LOCK_BUSY, EXIT_REFUSED, CountsHandler

IDENTITY = "counts-v1"
REPO = Path(__file__).resolve().parents[1]
DATA = b"bytes\n"
BOUNDARIES = (
    "object_staged",
    "object_published",
    "archive_committed",
    "todo_staged",
    "todo_published",
    "source_unlinked",
)
CRASH = (
    "import os,sys; from pathlib import Path; from dsl41.file_processing import archive; "
    "archive(Path(sys.argv[1]),'counts-v1', "
    "failpoint=lambda name: os._exit(31) if name == sys.argv[2] else None)"
)
PROCESS_BOUNDARIES = ("claimed", "handler_returned", "outcome_committed")
PROCESS_CRASH = (
    "import os,sys; from pathlib import Path; from dsl41.file_processing import process; "
    "from examples.file_processing.worker import CountsHandler; root=Path(sys.argv[1]); "
    "process(root,'counts-v1',CountsHandler(root/'output'), "
    "failpoint=lambda name: os._exit(31) if name == sys.argv[2] else None)"
)


def query(root: Path, sql: str) -> list[sqlite3.Row]:
    db = sqlite3.connect(root / "ledger.sqlite3")
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def deliver(root: Path, name: str, data: bytes) -> None:
    (root / "inbox").mkdir(parents=True, exist_ok=True)
    (root / "inbox" / name).write_bytes(data)


def handler(root: Path) -> CountsHandler:
    return CountsHandler(root / "output")


def published(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.name != fp.STAGE)


def staged(root: Path, name: str) -> list[str]:
    return sorted(path.name for path in (root / name / fp.STAGE).iterdir())


def tree(root: Path, ledger: bool = True) -> list[tuple[object, ...]]:
    """Every entry with its type, inode and bytes; lock files excluded."""
    files: list[tuple[object, ...]] = []
    for path in sorted(root.rglob("*")):
        if path.suffix == ".lock" or (not ledger and path.name.startswith("ledger.sqlite3")):
            continue
        info = path.lstat()
        data = path.read_bytes() if stat.S_ISREG(info.st_mode) else b""
        files.append((str(path.relative_to(root)), stat.S_IFMT(info.st_mode), info.st_ino, data))
    return files


def snapshot(root: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    """The tree without ledger files, and every ledger row."""
    files = tree(root, ledger=False)
    rows = [tuple(row) for row in query(root, "SELECT * FROM receipts ORDER BY seq")]
    rows += [tuple(row) for row in query(root, "SELECT * FROM work ORDER BY work_key")]
    return files, rows


def cli(root: Path, role: str, *extra: str) -> subprocess.CompletedProcess[str]:
    worker = [sys.executable, "-m", "examples.file_processing.worker", role]
    return subprocess.run(
        [*worker, "--root", str(root), *extra],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )


def operator_cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    operator = [sys.executable, "-m", "examples.file_processing.operate"]
    return subprocess.run(
        [*operator, "--root", str(root), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )


def assert_drained(root: Path) -> None:
    assert not list((root / "inbox").iterdir())
    assert staged(root, "objects") == staged(root, "todo") == []
    assert published(root / "objects") == [hashlib.sha256(DATA).hexdigest()]
    assert published(root / "todo") == []
    receipts = query(root, "SELECT state, cleaned FROM receipts")
    assert receipts and all(tuple(row) == ("FILE_ARCHIVED", 1) for row in receipts)
    assert [row[0] for row in query(root, "SELECT state FROM work")] == ["DONE"]
    assert len(list((root / "output").glob("*.json"))) == 1
    check_counts_outputs(root)


CASES = [
    pytest.param(stage, duplicate, first, id=f"{stage}-{'dup' if duplicate else 'new'}-{first}")
    for stage in BOUNDARIES
    for duplicate in (False, True)
    for first in ("archive", "process")
    if not (duplicate and stage == "todo_staged")
]


@pytest.mark.parametrize("stage,duplicate,first", CASES)
def test_every_archival_boundary_converges(
    tmp_path: Path, stage: str, duplicate: bool, first: str
) -> None:
    """A duplicate of DONE work stages no TODO, so it has no todo_staged boundary."""
    if duplicate:
        deliver(tmp_path, "earlier", DATA)
        fp.archive(tmp_path, IDENTITY)
        assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).completed == 1
    deliver(tmp_path, "delivery", DATA)
    source = tmp_path / "inbox" / "delivery"
    inode = source.stat().st_ino
    child = subprocess.run(
        [sys.executable, "-c", CRASH, str(tmp_path), stage],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 31, child.stderr
    at = BOUNDARIES.index(stage)
    committed = at >= BOUNDARIES.index("archive_committed")
    todo = not duplicate and at >= BOUNDARIES.index("todo_published")
    if stage == "source_unlinked":
        assert not list((tmp_path / "inbox").iterdir())
    else:
        assert [path.name for path in (tmp_path / "inbox").iterdir()] == ["delivery"]
        info = source.stat()
        assert (info.st_ino, info.st_nlink, source.read_bytes()) == (inode, 1, DATA)
    assert len(staged(tmp_path, "objects")) == (stage == "object_staged")
    assert len(staged(tmp_path, "todo")) == (stage == "todo_staged")
    assert len(published(tmp_path / "objects")) == int(duplicate or stage != "object_staged")
    assert len(published(tmp_path / "todo")) == todo
    receipt = query(tmp_path, "SELECT state, cleaned FROM receipts WHERE name='delivery'")[0]
    assert tuple(receipt) == ("FILE_ARCHIVED" if committed else "FILE_RECEIVED", 0)
    work = [row[0] for row in query(tmp_path, "SELECT state FROM work")]
    assert work == (["DONE"] if duplicate else ["READY"] if committed else [])

    if first == "process":
        report = fp.process(tmp_path, IDENTITY, handler(tmp_path))
        assert report.completed == todo
        assert report.remaining == int(committed and not duplicate and not todo)
    assert fp.archive(tmp_path, IDENTITY).selected == int(not committed)
    fp.process(tmp_path, IDENTITY, handler(tmp_path))
    assert_drained(tmp_path)
    before = snapshot(tmp_path)
    assert fp.archive(tmp_path, IDENTITY).selected == 0
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).selected == 0
    assert snapshot(tmp_path) == before


def test_failpoints_follow_the_documented_boundaries_and_inbox_is_only_read(
    tmp_path: Path,
) -> None:
    deliver(tmp_path, "delivery", DATA)
    source = tmp_path / "inbox" / "delivery"
    inode = source.stat().st_ino
    seen: list[tuple[str, list[str], int]] = []

    def observe(stage: str) -> None:
        names = sorted(path.name for path in (tmp_path / "inbox").iterdir())
        seen.append((stage, names, source.stat().st_nlink if names else 0))
        if names:
            assert source.stat().st_ino == inode and source.read_bytes() == DATA

    fp.archive(tmp_path, IDENTITY, failpoint=observe)
    assert [stage for stage, _, _ in seen] == list(BOUNDARIES)
    for stage, names, links in seen:
        assert (names, links) == (([], 0) if stage == "source_unlinked" else (["delivery"], 1))
    obj = tmp_path / "objects" / hashlib.sha256(DATA).hexdigest()
    assert obj.stat().st_nlink == 1 and obj.stat().st_ino != inode


def test_source_retained_until_the_handover_is_published(tmp_path: Path) -> None:
    deliver(tmp_path, "delivery", DATA)

    def lose_todo(stage: str) -> None:
        if stage == "todo_published":
            for name in published(tmp_path / "todo"):
                (tmp_path / "todo" / name).unlink()

    with pytest.raises(fp.ProtocolError, match="handover incomplete"):
        fp.archive(tmp_path, IDENTITY, failpoint=lose_todo)
    assert (tmp_path / "inbox" / "delivery").read_bytes() == DATA
    assert tuple(query(tmp_path, "SELECT state, cleaned FROM receipts")[0]) == ("FILE_ARCHIVED", 0)
    assert fp.archive(tmp_path, IDENTITY).selected == 0
    assert len(published(tmp_path / "todo")) == 1
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).completed == 1
    assert_drained(tmp_path)


def test_processor_completes_while_archiver_pauses_after_todo_publication(
    tmp_path: Path,
) -> None:
    deliver(tmp_path, "delivery", DATA)
    script = (
        "import sys; from pathlib import Path; from dsl41.file_processing import archive\n"
        "def pause(name):\n"
        "    if name == 'todo_published':\n"
        "        print('paused', flush=True); sys.stdin.readline()\n"
        "print(archive(Path(sys.argv[1]), 'counts-v1', failpoint=pause).completed, flush=True)\n"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "paused"
        report = fp.process(tmp_path, IDENTITY, handler(tmp_path))
        assert (report.completed, report.remaining) == (1, 0)
        assert (tmp_path / "inbox" / "delivery").exists()
        out, err = child.communicate("resume\n", timeout=30)
    assert child.returncode == 0, err
    assert out.strip() == "1"
    assert_drained(tmp_path)


def test_archiver_runs_while_processor_pauses_inside_its_claim(tmp_path: Path) -> None:
    deliver(tmp_path, "a", DATA)
    fp.archive(tmp_path, IDENTITY)
    script = (
        "import sys; from pathlib import Path; from dsl41.file_processing import process\n"
        "from examples.file_processing.worker import CountsHandler\n"
        "class Pause(CountsHandler):\n"
        "    def process(self, item):\n"
        "        print('claimed', flush=True); sys.stdin.readline()\n"
        "        return super().process(item)\n"
        "root = Path(sys.argv[1])\n"
        "print(process(root, 'counts-v1', Pause(root / 'output')).completed, flush=True)\n"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "claimed"
        claimed = tuple(query(tmp_path, "SELECT * FROM work")[0])
        assert claimed[4] == "RUNNING"
        todo = published(tmp_path / "todo")
        deliver(tmp_path, "b", DATA)
        report = fp.archive(tmp_path, IDENTITY)
        assert (report.completed, report.duplicates) == (1, 1)
        assert not list((tmp_path / "inbox").iterdir())
        assert published(tmp_path / "todo") == todo
        assert tuple(query(tmp_path, "SELECT * FROM work")[0]) == claimed
        out, err = child.communicate("resume\n", timeout=30)
    assert child.returncode == 0, err
    assert out.strip() == "1"
    assert_drained(tmp_path)


@pytest.mark.parametrize("backlog", [False, True])
def test_repeated_noop_cli_runs_change_nothing(tmp_path: Path, backlog: bool) -> None:
    deliver(tmp_path, "a", DATA)
    fp.archive(tmp_path, IDENTITY)
    if not backlog:
        fp.process(tmp_path, IDENTITY, handler(tmp_path))
    before = snapshot(tmp_path)
    roles = ["archive"] * 3 if backlog else ["archive", "process"] * 3
    for role in roles:
        run = cli(tmp_path, role)
        assert run.returncode == 0, run.stderr
        summary = json.loads(run.stdout)
        assert (summary["selected"], summary["remaining"], summary["stopped"]) == (0, 0, False)
        assert snapshot(tmp_path) == before
    check_counts_outputs(tmp_path)


@pytest.mark.parametrize("role", ["archive", "process"])
def test_lock_busy_is_a_nonzero_cli_exit(tmp_path: Path, role: str) -> None:
    deliver(tmp_path, "a", DATA)
    fp.archive(tmp_path, IDENTITY)
    before = snapshot(tmp_path)
    script = (
        "import os,sys; from pathlib import Path; from dsl41.file_processing import _lock; "
        "lock=_lock(Path(sys.argv[1]),sys.argv[2]); lock.__enter__(); "
        "print('locked',flush=True); sys.stdin.readline(); os._exit(19)"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), role],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        busy = cli(tmp_path, role)
        assert (busy.returncode, busy.stdout) == (EXIT_LOCK_BUSY, "")
        assert f"{role}.lock busy" in busy.stderr
        assert snapshot(tmp_path) == before
        child.communicate("exit\n", timeout=30)
    assert cli(tmp_path, role).returncode == 0


@pytest.mark.parametrize("role", ["archive", "process"])
def test_refusal_is_a_distinct_cli_exit(tmp_path: Path, role: str) -> None:
    deliver(tmp_path, "a", DATA)
    fp.archive(tmp_path, IDENTITY)
    (tmp_path / "todo" / "stray.txt").write_bytes(b"foreign")
    (tmp_path / "objects" / fp.STAGE / "stray").write_bytes(b"foreign")
    before = snapshot(tmp_path)
    refused = cli(tmp_path, role)
    assert (refused.returncode, refused.stdout) == (EXIT_REFUSED, "")
    assert refused.stderr.startswith(f"{role} refused: ")
    assert len(refused.stderr.splitlines()) == 1
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("role", ["archive", "process"])
@pytest.mark.parametrize(
    "damage",
    ["malformed", "invalid_utf8", "symlink", "ledger", "column", "ledger_dir", "identity"],
)
def test_every_refusal_exits_65_without_a_summary(tmp_path: Path, role: str, damage: str) -> None:
    deliver(tmp_path, "a", DATA)
    fp.archive(tmp_path, IDENTITY)
    todo = tmp_path / "todo" / published(tmp_path / "todo")[0]
    extra: tuple[str, ...] = ()
    if damage == "malformed":
        todo.write_bytes(b"{")
    elif damage == "invalid_utf8":
        todo.write_bytes(b"\xff")
    elif damage == "symlink":
        copy = tmp_path / "elsewhere.json"
        copy.write_bytes(todo.read_bytes())
        todo.unlink()
        todo.symlink_to(copy)
    elif damage == "ledger":
        (tmp_path / "ledger.sqlite3").write_bytes(b"not a database" * 100)
    elif damage == "column":
        db = sqlite3.connect(tmp_path / "ledger.sqlite3")
        db.execute("ALTER TABLE identity RENAME COLUMN processor_id TO renamed")
        db.commit()
        db.close()
    elif damage == "ledger_dir":
        (tmp_path / "ledger.sqlite3").unlink()
        (tmp_path / "ledger.sqlite3").mkdir()
    else:
        extra = ("--processor-id", "")
    before = tree(tmp_path)
    refused = cli(tmp_path, role, *extra)
    assert (refused.returncode, refused.stdout) == (EXIT_REFUSED, ""), refused.stderr
    assert refused.stderr.startswith(f"{role} refused: ")
    assert len(refused.stderr.splitlines()) == 1
    assert tree(tmp_path) == before


def test_peer_creating_the_ledger_does_not_refuse_a_fresh_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other role creates the ledger while this role reads lost-ledger evidence."""
    original = fp._established
    peers: list[sqlite3.Connection] = []

    def other_role_opens_first(root: Path) -> bool:
        peer = sqlite3.connect(root / "ledger.sqlite3", isolation_level=None)
        peers.append(peer)
        peer.execute("BEGIN IMMEDIATE")
        peer.execute("CREATE TABLE IF NOT EXISTS identity (processor_id TEXT NOT NULL)")
        assert any(root.glob("ledger.sqlite3-*"))
        try:
            return original(root)
        finally:
            peer.commit()

    monkeypatch.setattr(fp, "_established", other_role_opens_first)
    deliver(tmp_path, "a", DATA)
    try:
        assert fp.archive(tmp_path, IDENTITY).completed == 1
    finally:
        for peer in peers:
            peer.close()
    monkeypatch.undo()
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).completed == 1
    assert_drained(tmp_path)


def test_busy_ledger_is_not_a_refusal(tmp_path: Path) -> None:
    """Contention past the timeout stays OperationalError, so the CLI exits 1, not 65."""
    ledger = tmp_path / "ledger.sqlite3"
    for code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        busy = sqlite3.OperationalError("database is locked")
        busy.sqlite_errorcode = code
        with pytest.raises(sqlite3.OperationalError) as raised:
            fp._ledger_failure(ledger, busy)
        assert raised.value is busy
    other = sqlite3.OperationalError("no such column")
    other.sqlite_errorcode = sqlite3.SQLITE_ERROR
    with pytest.raises(fp.ProtocolError, match="no such column"):
        fp._ledger_failure(ledger, other)
    uncoded = sqlite3.OperationalError("Could not decode to UTF-8 column 'metadata'")
    assert not hasattr(uncoded, "sqlite_errorcode")
    with pytest.raises(fp.ProtocolError, match="decode"):
        fp._ledger_failure(ledger, uncoded)


def test_ledger_busy_after_opening_exits_1_with_the_reason(tmp_path: Path) -> None:
    """A peer holds the write lock from the claim on; the outcome commit times out."""
    deliver(tmp_path, "a", DATA)
    fp.archive(tmp_path, IDENTITY)
    script = (
        "import sys; from examples.file_processing import worker\n"
        "class Pause(worker.CountsHandler):\n"
        "    def process(self, item):\n"
        "        print('claimed', flush=True); sys.stdin.readline()\n"
        "        return super().process(item)\n"
        "worker.CountsHandler = Pause\n"
        "sys.argv = ['worker', 'process', '--root', sys.argv[1]]\n"
        "worker.main()\n"
    )
    peer = sqlite3.connect(tmp_path / "ledger.sqlite3", isolation_level=None)
    try:
        with subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=REPO,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as child:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "claimed"
            peer.execute("BEGIN IMMEDIATE")
            out, err = child.communicate("resume\n", timeout=60)
            peer.rollback()
    finally:
        peer.close()
    assert (child.returncode, out) == (1, "")
    assert err.splitlines()[-1] == "sqlite3.OperationalError: database is locked"
    assert tuple(query(tmp_path, "SELECT state, attempt FROM work")[0]) == ("RUNNING", 1)
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).completed == 1
    assert_drained(tmp_path)


def test_handover_check_takes_the_write_lock(tmp_path: Path) -> None:
    deliver(tmp_path, "a", DATA)
    fp.archive(tmp_path, IDENTITY)
    key = query(tmp_path, "SELECT work_key FROM work")[0][0]
    peer = sqlite3.connect(tmp_path / "ledger.sqlite3", isolation_level=None)
    db = sqlite3.connect(tmp_path / "ledger.sqlite3", isolation_level=None, timeout=0.1)
    try:
        peer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            fp._handed_over(tmp_path, db, key)
        peer.rollback()
        assert fp._handed_over(tmp_path, db, key)
    finally:
        db.close()
        peer.close()


@pytest.mark.parametrize(
    "directory,entry,kind",
    [("objects", "stray", "file"), ("todo", "stray", "file"), ("objects", "object-x", "dir")],
)
def test_foreign_staging_entry_is_refused(
    tmp_path: Path, directory: str, entry: str, kind: str
) -> None:
    deliver(tmp_path, "a", DATA)
    fp.archive(tmp_path, IDENTITY)
    target = tmp_path / directory / fp.STAGE / entry
    if kind == "dir":
        target.mkdir()
    else:
        target.write_bytes(b"foreign")
    with pytest.raises(fp.ProtocolError, match="staging directory|not a regular file"):
        fp.archive(tmp_path, IDENTITY)
    assert target.exists()


def test_processor_refuses_foreign_todo_entry_but_skips_staging(tmp_path: Path) -> None:
    deliver(tmp_path, "a", DATA)
    fp.archive(tmp_path, IDENTITY)
    (tmp_path / "todo" / fp.STAGE / "todo-abandoned").write_bytes(b"partial")
    (tmp_path / "todo" / "stray.txt").write_bytes(b"foreign")
    with pytest.raises(fp.ProtocolError, match="invalid TODO filename"):
        fp.process(tmp_path, IDENTITY, handler(tmp_path))
    (tmp_path / "todo" / "stray.txt").unlink()
    assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).completed == 1


def test_staging_path_must_be_a_real_directory(tmp_path: Path) -> None:
    fp.archive(tmp_path, IDENTITY)
    (tmp_path / "todo" / fp.STAGE).rmdir()
    (tmp_path / "todo" / fp.STAGE).write_bytes(b"not a directory")
    with pytest.raises(fp.ProtocolError, match="not a real directory"):
        fp.process(tmp_path, IDENTITY, handler(tmp_path))


@pytest.mark.parametrize("role", ["archive", "process"])
@pytest.mark.parametrize(
    "evidence", ["none", "object", "todo", "object_stage", "todo_stage", "sidecar"]
)
def test_lost_ledger_evidence_excludes_empty_staging(
    tmp_path: Path, role: str, evidence: str
) -> None:
    fp._layout(tmp_path, IDENTITY)
    assert staged(tmp_path, "objects") == staged(tmp_path, "todo") == []
    path = {
        "none": None,
        "object": tmp_path / "objects" / ("0" * 64),
        "todo": tmp_path / "todo" / f"{'0' * 64}.json",
        "object_stage": tmp_path / "objects" / fp.STAGE / "object-abandoned",
        "todo_stage": tmp_path / "todo" / fp.STAGE / "todo-abandoned",
        "sidecar": tmp_path / "ledger.sqlite3-journal",
    }[evidence]
    if path is not None:
        path.write_bytes(b"evidence")

    def run() -> fp.Summary:
        if role == "archive":
            return fp.archive(tmp_path, IDENTITY)
        return fp.process(tmp_path, IDENTITY, handler(tmp_path))

    if path is None:
        assert run().selected == 0
        assert (tmp_path / "ledger.sqlite3").exists()
    else:
        with pytest.raises(fp.ProtocolError, match="missing ledger"):
            run()
        assert not (tmp_path / "ledger.sqlite3").exists()


class WithoutReconciler:
    def __init__(self, output: Path) -> None:
        self.counts = CountsHandler(output)

    def process(self, item: fp.WorkItem) -> fp.Result:
        return self.counts.process(item)


def moves(root: Path) -> list[tuple[str, str]]:
    [rows] = record(root).values()
    return [(row["chart_row"], row["origin"]) for row in rows]


@pytest.mark.parametrize("reconciler", [True, False])
@pytest.mark.parametrize("stage", PROCESS_BOUNDARIES)
def test_every_processing_boundary_converges(tmp_path: Path, stage: str, reconciler: bool) -> None:
    """A run killed at each durable boundary converges on the next runs: the item ends DONE
    after reconciliation with the sample handler, or UNKNOWN without a reconciler."""
    deliver(tmp_path, "delivery", DATA)
    fp.archive(tmp_path, IDENTITY)
    child = subprocess.run(
        [sys.executable, "-c", PROCESS_CRASH, str(tmp_path), stage],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 31, child.stderr
    committed = stage == "outcome_committed"
    work = tuple(query(tmp_path, "SELECT state, attempt FROM work")[0])
    assert work == ("DONE" if committed else "RUNNING", 1)
    assert len(published(tmp_path / "todo")) == 1
    assert len(list((tmp_path / "output").glob("*.json"))) == int(stage != "claimed")
    callback = handler(tmp_path) if reconciler else WithoutReconciler(tmp_path / "output")
    selected = [fp.process(tmp_path, IDENTITY, callback).selected for _ in range(2)]
    claim, done = ("file_process.01", "handler"), ("file_process.02", "handler")
    interrupted = ("file_process.04", "reconciler")
    if committed:
        state, expected, counts = "DONE", [claim, done], [0, 0]
    elif not reconciler:
        unknown = ("file_process.07", "reconciler")
        state, expected, counts = "UNKNOWN", [claim, interrupted, unknown, unknown], [1, 1]
    elif stage == "handler_returned":
        reconciled = ("file_process.05", "reconciler")
        state, expected, counts = "DONE", [claim, interrupted, reconciled], [1, 0]
    else:
        retry = ("file_process.06", "reconciler")
        state, expected, counts = "DONE", [claim, interrupted, retry, claim, done], [1, 1]
    assert (moves(tmp_path), selected) == (expected, counts)
    assert query(tmp_path, "SELECT state FROM work")[0][0] == state
    assert len(published(tmp_path / "todo")) == int(state == "UNKNOWN")
    check_counts_outputs(tmp_path)
    if state == "DONE":
        deliver(tmp_path, "again", DATA)
        fp.archive(tmp_path, IDENTITY)
        assert_drained(tmp_path)
        before = snapshot(tmp_path)
        assert fp.process(tmp_path, IDENTITY, handler(tmp_path)).selected == 0
        assert snapshot(tmp_path) == before


def refusals(root: Path, key: str) -> list[tuple[str, subprocess.CompletedProcess[str]]]:
    """Every role and operator command, run once through its CLI."""
    rule = ["rule", "--key", key, "--event", "rejected", "--expect-state", "READY"]
    return [
        ("archive", cli(root, "archive")),
        ("process", cli(root, "process")),
        ("list", operator_cli(root, "list")),
        ("show", operator_cli(root, "show", "--key", key)),
        ("rule", operator_cli(root, *rule, "--expect-attempt", "0", "--reason", "x")),
    ]


@pytest.mark.parametrize("damage", ["page", "text"])
def test_ledger_damage_after_opening_is_a_refusal_in_every_role(
    tmp_path: Path, damage: str
) -> None:
    """The damage is beyond what _open reads: the work table's root page, or one byte of
    one work row's metadata, which is then not UTF-8. Each CLI exits 65 without a summary."""
    for index in range(3):
        deliver(tmp_path, f"d{index}", f"{index}\n".encode())
    fp.archive(tmp_path, IDENTITY)
    fp.process(tmp_path, IDENTITY, handler(tmp_path), fp.Policy(batch="one"))
    key = fp.processing_key(IDENTITY, hashlib.sha256(b"1\n").hexdigest())
    ledger = tmp_path / "ledger.sqlite3"
    data = bytearray(ledger.read_bytes())
    if damage == "page":
        page_size = query(tmp_path, "PRAGMA page_size")[0][0]
        roots = {
            row[0]: row[1] for row in query(tmp_path, "SELECT name, rootpage FROM sqlite_master")
        }
        assert roots["work"] not in {roots["sqlite_sequence"], roots["identity"], 1}
        offset = (roots["work"] - 1) * page_size
        data[offset : offset + page_size] = b"\xff" * page_size
    else:
        cell = b'{"delivery_name":"d1"}'
        assert data.count(cell) == 1
        data[data.index(cell) + len(cell) - 3] = 0xFF
    ledger.write_bytes(bytes(data))
    fp._open(tmp_path, IDENTITY).close()
    before = tree(tmp_path)
    for command, run in refusals(tmp_path, key):
        assert (run.returncode, run.stdout) == (EXIT_REFUSED, ""), (command, run.stderr)
        assert run.stderr.startswith(f"{command} refused: unusable ledger"), run.stderr
        assert len(run.stderr.splitlines()) == 1
    assert tree(tmp_path) == before


SLICE2_SCHEMA = (
    "CREATE TABLE identity (processor_id TEXT NOT NULL)",
    "CREATE TABLE receipts (seq INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,"
    "state TEXT NOT NULL CHECK(state IN ('FILE_RECEIVED','FILE_ARCHIVED')),"
    "content_hash TEXT, work_key TEXT, cleaned INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE work (work_key TEXT PRIMARY KEY, content_hash TEXT UNIQUE NOT NULL,"
    "receipt_seq INTEGER NOT NULL REFERENCES receipts(seq), metadata TEXT NOT NULL,"
    "state TEXT NOT NULL CHECK(state IN ('READY','RUNNING','DONE','UNKNOWN')),"
    "evidence TEXT, detail TEXT)",
)


def test_prior_ledger_schema_is_refused_without_a_change(tmp_path: Path) -> None:
    """No migration: a slice 2 ledger is refused by every role and command (exit 65)."""
    fp._layout(tmp_path, IDENTITY)
    content_hash = hashlib.sha256(DATA).hexdigest()
    key = fp.processing_key(IDENTITY, content_hash)
    db = sqlite3.connect(tmp_path / "ledger.sqlite3")
    for ddl in SLICE2_SCHEMA:
        db.execute(ddl)
    db.execute("INSERT INTO identity VALUES (?)", (IDENTITY,))
    db.execute("INSERT INTO receipts VALUES (1,'a','FILE_ARCHIVED',?,?,1)", (content_hash, key))
    db.execute("INSERT INTO work VALUES (?,?,1,'{}','READY',NULL,NULL)", (key, content_hash))
    db.commit()
    db.close()
    before = tree(tmp_path)
    for command, run in refusals(tmp_path, key):
        assert (run.returncode, run.stdout) == (EXIT_REFUSED, ""), (command, run.stderr)
        assert run.stderr.startswith(f"{command} refused: unsupported ledger schema: table work")
        assert len(run.stderr.splitlines()) == 1
    assert tree(tmp_path) == before


def test_checker_validates_an_uncertain_effect(tmp_path: Path) -> None:
    """After a handler_returned crash without a reconciler the output stays UNKNOWN; its
    bytes are still checked, and an operator's completed ruling is checked for them too."""
    deliver(tmp_path, "delivery", DATA)
    fp.archive(tmp_path, IDENTITY)
    child = subprocess.run(
        [sys.executable, "-c", PROCESS_CRASH, str(tmp_path), "handler_returned"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 31, child.stderr
    assert fp.process(tmp_path, IDENTITY, WithoutReconciler(tmp_path / "output")).unknown == 1
    check_counts_outputs(tmp_path)
    [output] = (tmp_path / "output").glob("*.json")
    good = output.read_bytes()
    output.chmod(0o644)
    output.write_bytes(b"not the count\n")
    with pytest.raises(AssertionError, match="UNKNOWN"):
        check_counts_outputs(tmp_path)
    output.write_bytes(good)
    fp.rule_work(
        tmp_path,
        IDENTITY,
        output.stem,
        "completed",
        expect_state="UNKNOWN",
        expect_attempt=1,
        actor="op",
        evidence="operator compared the count file",
    )
    check_counts_outputs(tmp_path)


def test_terminal_states_are_the_process_chart_finals() -> None:
    assert fp.TERMINAL == {"DONE", "REJECTED"}
    assert not {row.source for row in PROCESS.rows} & fp.TERMINAL
    assert all(fp._terminal(state) for state in fp.TERMINAL)
    assert not any(fp._terminal(state) for state in ("READY", "RUNNING", "UNKNOWN"))


def test_root_has_no_shared_staging_directory(tmp_path: Path) -> None:
    deliver(tmp_path, "a", DATA)
    fp.archive(tmp_path, IDENTITY)
    fp.process(tmp_path, IDENTITY, handler(tmp_path))
    assert not (tmp_path / "tmp").exists()
    assert os.path.isdir(tmp_path / "objects" / fp.STAGE)
    assert os.path.isdir(tmp_path / "todo" / fp.STAGE)
